"""The file becomes numbered lines (``L001 ...``), with no model: the addresses every
value of the profile cites.

- **PDF**: Azure Document Intelligence ``prebuilt-layout`` (the official SDK, the
  ``docintel`` extra), Markdown out: reading order, columns, OCR for scans and Arabic
  come from it, and its HTML tables become one Markdown row a line (markdownify), so a
  citation names a row. Connection drops, 429 and 5xx are retried by azure-core's own
  policy. Only the first ``max_pages`` pages are sent for reading, and each read deletes
  the file and its result from Azure straight after (Azure keeps them 24 hours otherwise).
- **Word and plain text**: asas-cv's reader (document order, tables under their
  headings), plus the Word page headers it leaves out, where a CV often keeps the name
  and contact lines, put first.

A file is turned away before anything reads it when its first bytes are not the type its
name claims, when a Word file unpacks past ``MAX_UNPACKED`` (a zip bomb), or past
``max_pages``; a long file must
look like a CV (two of the headings nearly every CV has); a file with almost no
letters holds no CV. Each refusal is a :class:`Refused` with a stable code.
"""

from __future__ import annotations

import html
import io
import logging
import re
from collections import Counter
import unicodedata
import zipfile
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from asas_cv import CODES as _READ_CODES

if TYPE_CHECKING:  # pragma: no cover
    from .headings import Headings
    from .seams import PdfReader

#: what a refusal's ``code`` can be: asas-cv's own, then this package's
CODES = (
    *_READ_CODES,
    "document_too_many_pages",
    "document_too_little_text",
    "document_not_a_cv",
    "reader_not_configured",
    "reader_unavailable",
)

log = logging.getLogger(__name__)
ZERO_WIDTH = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")  # zero-width and direction marks
#: under this many letters the file holds no CV to read (a blank template, a cover page)
MIN_LETTERS = 100
#: characters; a CV line is rarely a third of that
LONG_LINE = 500
#: of 1,500 real PDF CVs the median ran 3 pages, 1 in 20 over 13 and 8 over 30. Past
#: MAX_PAGES a file is turned away before anything reads it; past CHECK_PAGES it must look like a CV
MAX_PAGES, CHECK_PAGES = 40, 10
#: Document Intelligence's page header and footer comments: <!-- PageHeader="..." -->
_PAGE_EDGE = re.compile(r'<!--\s*Page(Header|Footer)="(.*?)"\s*-->', re.DOTALL)
#: Latin words of three letters or more and four-digit numbers (years), the marks the text
#: layer and the reader's text are compared on; Arabic in a PDF's text layer is often in
#: presentation forms or reversed, so it is left out of the comparison
_LAYER_WORD = re.compile(r"[a-z]{3,}|\d{4}")
#: a read text lacking any year the text layer has, or this share of its words (and at
#: least LAYER_MIN of them), gets a warning: one lost line of a two-page CV, an employer
#: and its dates, was 5 of 315 words
LAYER_MISSING, LAYER_MIN = 0.01, 3
#: a Word file is a zip: of 1,273 real Word CVs the largest unpacked to 26 MB in 46 parts.
#: zipfile stops each part at its stated size, so the stated sizes bound what reading takes
MAX_UNPACKED, MAX_PARTS = 100 * 2**20, 1000


class Refused(Exception):
    """The file is not one this step reads into a CV's lines; ``code`` (one of
    :data:`CODES`) says why."""

    def __init__(self, code: str, message: str):
        if code not in CODES:
            raise ValueError(f"unknown refusal code {code!r}")
        super().__init__(message)
        self.code = code


@dataclass
class Lines:
    """A CV as numbered lines, and how it was read."""

    lines: list[str]
    reader: str
    pages: int = 0
    warnings: list[str] = field(default_factory=list)

    def numbered(self) -> str:
        width = max(3, len(str(len(self.lines))))
        return "\n".join(f"L{i:0{width}d} {line}" for i, line in enumerate(self.lines, 1))

    def part(self, nums: list[int]) -> str:
        """Some of the lines, numbered as in the whole text."""
        width = max(3, len(str(len(self.lines))))
        return "\n".join(f"L{n:0{width}d} {self.line(n)}" for n in nums)

    def line(self, n: int) -> str | None:
        return self.lines[n - 1] if 1 <= n <= len(self.lines) else None


@dataclass(frozen=True)
class LayoutReader:
    """Azure Document Intelligence as a :class:`~asas_cv_profile.seams.PdfReader`, with
    the endpoint of the host's resource and its key or an Entra ID ``credential`` (an
    azure-identity credential, such as a managed identity); the package reads no settings.

    Azure keeps the file and its result for 24 hours unless asked to delete them, so each
    read deletes its own straight after, a failed one too. Only the first ``max_pages`` pages are read (and billed)."""

    endpoint: str
    key: str | None = field(default=None, repr=False)  # never printed, logged or kept in a result
    model: str = "prebuilt-layout"
    # Microsoft recommends Entra ID with a managed identity for production, so the app
    # stores no key; a key suits local runs. See "Use API keys with caution" in
    # https://learn.microsoft.com/azure/ai-services/document-intelligence/quickstarts/get-started-sdks-rest-api
    credential: Any = field(default=None, repr=False)
    max_pages: int = MAX_PAGES
    # seconds a read may take before the file is refused as reader_unavailable (a later run reads it
    # again): azure-core's poller waits with no limit, and one stuck operation once held a CV for 11
    # minutes (29 Oct 2026); its polling runs in a thread, so result(timeout=...) returns on time
    read_timeout: float = 300

    def __post_init__(self) -> None:
        if (self.key is None) == (self.credential is None):
            raise ValueError("LayoutReader takes a key or a credential, one of the two")

    def read(self, data: bytes) -> Lines:
        try:
            from azure.ai.documentintelligence import DocumentIntelligenceClient
            from azure.ai.documentintelligence.models import DocumentContentFormat
            from azure.core.credentials import AzureKeyCredential
            from azure.core.exceptions import AzureError
        except ImportError:
            raise Refused("reader_not_configured", "Document Intelligence needs the docintel extra: pip install 'asas-cv-profile[docintel]'") from None

        # azure-core's own retry policy: connection drops, 429 and 5xx, six times each
        # (retry_status, or 429 and 5xx get its default of three), waiting 4, 8, 16, 32
        # and 60 seconds, about two minutes in all. Past that the file is refused as
        # reader_unavailable, and a later run reads it again.
        credential = AzureKeyCredential(self.key) if self.key is not None else self.credential
        with DocumentIntelligenceClient(self.endpoint, credential, retry_total=6, retry_status=6,
                                        retry_backoff_factor=2, retry_backoff_max=60) as client:
            poller = None
            try:
                # without a Retry-After from Azure the SDK waits 30 seconds between checks;
                # Microsoft's limits page asks for no more than one every 2 seconds
                poller = client.begin_analyze_document(self.model, body=data, pages=f"1-{self.max_pages}",
                                                       output_content_format=DocumentContentFormat.MARKDOWN,
                                                       polling_interval=2)
                result = poller.result(timeout=self.read_timeout)
                if not poller.done():
                    raise Refused("reader_unavailable", f"Document Intelligence did not finish within {self.read_timeout:.0f} seconds.")
            except AzureError as exc:
                raise Refused("reader_unavailable", f"Document Intelligence could not be reached or failed: {type(exc).__name__}.") from None
            finally:  # read or failed, once Azure holds the file it is deleted now
                if poller is not None:
                    try:
                        client.delete_analyze_result(self.model, poller.details["operation_id"])
                    except AzureError as exc:  # the read stands; Azure deletes it after 24 hours anyway
                        log.warning("Document Intelligence kept this result (it deletes it after 24 hours): %s", type(exc).__name__)
        return Lines(clean(layout_markdown(result.content or "").splitlines()), "docintel", pages=len(result.pages or []))


def clean(lines: list[str]) -> list[str]:
    out = []
    for raw in lines:
        s = ZERO_WIDTH.sub("", unicodedata.normalize("NFC", raw))
        s = re.sub(r"[ \t ]+", " ", s).strip()
        if s:
            out.append(s)
    return out


def page_count(data: bytes) -> int:
    from pdfminer.pdfpage import PDFPage

    try:
        return sum(1 for _ in PDFPage.get_pages(io.BytesIO(data)))
    except Exception:  # noqa: BLE001 a PDF pdfminer cannot walk is left to the reader to judge
        return 0


def read_cv(data: bytes, filename: str, *, pdf: PdfReader | None = None, headings: Headings | None = None,
            max_pages: int = MAX_PAGES, check_pages: int = CHECK_PAGES) -> Lines:
    """A CV file as numbered lines: a PDF through ``pdf`` (a
    :class:`~asas_cv_profile.seams.PdfReader`), Word and plain text through asas-cv.
    Raises :class:`Refused`."""
    from asas_cv import PDF_CONTENT_TYPE, ExtractionError, resolve_content_type

    try:
        content_type = resolve_content_type(filename, None)
    except ExtractionError as exc:
        raise Refused(exc.code, str(exc)) from None
    if not data:
        raise Refused("document_empty", "The file is empty.")
    _check_contents(data, content_type)
    if content_type == PDF_CONTENT_TYPE:
        if (pages := page_count(data)) > max_pages:
            raise Refused("document_too_many_pages", f"The PDF has {pages} pages; a CV is rarely over {max_pages}. Probably another document.")
        if pdf is None:
            raise Refused("reader_not_configured", "No PDF reader is configured (Document Intelligence).")
        text = pdf.read(data)
        missing, total = layer_missing(data, text.lines)
        years = sorted(w for w in missing if w.isdigit())
        if years or len(missing) >= max(LAYER_MIN, LAYER_MISSING * total):
            text.warnings.append(f"The read text lacks {len(missing)} of the {total} words the PDF's own text layer has"
                                 f"{', years among them' if years else ''}: a part of the CV may not have been read.")
    else:
        text = _asas_cv(data, filename)
    letters = sum(ch.isalpha() for line in text.lines for ch in line)
    if letters < MIN_LETTERS:
        raise Refused("document_too_little_text", f"The file holds only {letters} letters of text: no CV to read.")
    if text.pages > check_pages:
        from .headings import Headings

        if not (headings or Headings()).looks_like_cv(text.lines):
            raise Refused("document_not_a_cv", f"{text.pages} pages and fewer than two of the headings a CV has "
                          "(experience, education, skills, languages, certifications).")
    long = sum(len(line) > LONG_LINE for line in text.lines)
    if long:
        text.warnings.append(f"{long} of {len(text.lines)} lines run over {LONG_LINE} characters (a page read as one line): "
                             "a value checked against such a line proves little.")
    return text


def layer_missing(data: bytes, lines: list[str]) -> tuple[set[str], int]:
    """The words in the PDF's own text layer (pdfminer) the read lines lack, and how many
    words the layer has: (empty, 0) for a scan with no text layer, or a PDF pdfminer cannot
    read. A check, never a reader: the layer's reading order is not trusted."""
    from pdfminer.high_level import extract_text

    try:
        layer = extract_text(io.BytesIO(data), maxpages=MAX_PAGES)
    except Exception:  # noqa: BLE001 a layer pdfminer cannot read is no evidence either way
        return set(), 0
    want = set(_LAYER_WORD.findall(layer.casefold()))
    have = set(_LAYER_WORD.findall("\n".join(lines).casefold()))
    return want - have, len(want)


def _check_contents(data: bytes, content_type: str) -> None:
    """The first bytes must be the type the name claims, and a Word file must unpack to a
    size a CV has (OWASP's File Upload Cheat Sheet: the signature, not the name; the
    unpacked size, not the upload's)."""
    from asas_cv import DOCX_CONTENT_TYPE, PDF_CONTENT_TYPE

    if content_type == PDF_CONTENT_TYPE and b"%PDF-" not in data[:1024]:  # the window PDF readers allow
        raise Refused("document_unsupported_type", "The file is named .pdf but is not a PDF.")
    if content_type != DOCX_CONTENT_TYPE:
        return
    try:
        parts = zipfile.ZipFile(io.BytesIO(data)).infolist()
    except zipfile.BadZipFile:
        parts = []
    if not any(part.filename == "[Content_Types].xml" for part in parts):  # every Word file has one
        raise Refused("document_unsupported_type", "The file is named .docx but is not a Word document.")
    if len(parts) > MAX_PARTS or sum(part.file_size for part in parts) > MAX_UNPACKED:
        raise Refused("document_unreadable", f"The Word file unpacks to over {MAX_UNPACKED // 2**20} MB or "
                      f"{MAX_PARTS} parts; a CV is far smaller.")


def _asas_cv(data: bytes, filename: str) -> Lines:
    """The body through asas-cv; a .docx's page headers, which it leaves out, put first."""
    from asas_cv import (
        DOCX_CONTENT_TYPE,
        ExtractionError,
        extract_text,
        resolve_content_type,
    )

    try:
        body = extract_text(data, filename=filename)
    except ExtractionError as exc:
        raise Refused(exc.code, str(exc)) from None
    headers: list[str] = []
    if resolve_content_type(filename, None) == DOCX_CONTENT_TYPE:
        import docx

        try:
            for section in docx.Document(io.BytesIO(data)).sections:
                for p in section.header.paragraphs:
                    if p.text.strip() and p.text not in headers:
                        headers.append(p.text)
        except Exception:  # noqa: BLE001 asas-cv read the body; a header we cannot read costs only the header
            log.debug("a Word header could not be read", exc_info=True)
    return Lines(clean(headers + body.splitlines()), "asas-cv")


def layout_markdown(content: str) -> str:
    """Document Intelligence's Markdown, with its HTML tables made Markdown rows (one row
    a line, so a citation names a row, not a <td>) by markdownify, the converter
    MarkItDown itself uses, and its page comments and figure tags taken out."""
    from markdownify import markdownify

    content = re.sub(r"<table>.*?</table>", lambda m: "\n" + markdownify(m.group(0)).strip() + "\n", content, flags=re.DOTALL)
    # A page header or footer comes as a comment holding its text. A text repeated on
    # several pages is a running header (the name, a page title) and goes; any other is
    # CV text the layout put at a page's edge (a role's employer and dates at the top of
    # page 2) and stays as a line of its own.
    said = Counter(html.unescape(m.group(2)).strip() for m in _PAGE_EDGE.finditer(content))
    running = {t for t, k in said.items() if k > 1}

    def edge(m: re.Match) -> str:
        t = html.unescape(m.group(2)).strip()
        return "" if not t or t in running else f"\n{t}\n"

    content = _PAGE_EDGE.sub(edge, content)
    content = re.sub(r"<!--.*?-->", "", content, flags=re.DOTALL)
    return re.sub(r"</?figure[^>]*>|</?figcaption>", "", content)
