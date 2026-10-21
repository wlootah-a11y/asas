"""Lift a person's headshot out of their CV (PDF or .docx). Never raises.

Plenty of CVs carry a photo, and a product that parses the document already
holds the bytes: reading the face out of them costs one pass, asking the
person to upload the same face again costs them a step.

A hand-written PDF reader has three faults a real parser has already solved,
and the first version of this extractor had all three, passed 22 tests and
found nothing in any real CV:

- ``/Filter`` is a CHAIN, not one name. ReportLab writes
  ``[/ASCII85Decode /FlateDecode]``, so reading a single name finds nothing.
- ``/DecodeParms`` can carry a PREDICTOR, which means the samples are
  themselves PNG-filtered and taking them raw yields a sheared image. Word
  emits these.
- an object's dictionary is reached through the cross-reference table, not
  by scanning backwards from ``stream`` for ``obj``.

``PDFStream.get_data()`` (pdfminer) answers all three, so the only decisions
left here are which image is a FACE and in what shape to hand it on.

WHAT IT UNDERSTANDS. Image XObjects, page by page, including ones nested
inside a Form XObject. An already-encoded JPEG or PNG is passed through
untouched, so the common case decodes no pixels at all. Raw samples are
encoded as a JPEG: on a real 300x300 headshot PNG costs 17.4 ms and 137 KB
where JPEG at quality 90 costs 0.1 ms and 22 KB, and the photograph was a JPEG
before the producer expanded it into the PDF, so this is not a first
generation loss. DOCX is read as what it is, a zip with the pictures in
``word/media``.

Anything whose colour needs real management (CMYK samples, which PDF and
Pillow disagree about the polarity of) is SKIPPED rather than guessed at. A
wrong photo on a person's profile is worse than no photo.
"""

from __future__ import annotations

import logging
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from pdfminer.pdfdocument import PDFDocument
from pdfminer.pdfpage import PDFPage
from pdfminer.pdfparser import PDFParser
from pdfminer.pdftypes import PDFStream, resolve1
from PIL import Image

logger = logging.getLogger("asas_cv")

#: A headshot is not a favicon and not a page scan.
MIN_SIDE_PX = 100
#: Portrait through mildly landscape. A banner or a rule is far wider.
MIN_ASPECT = 0.5
MAX_ASPECT = 1.6
#: A full page scanned at 150 dpi is about 1240x1754. Nothing that big is a
#: headshot, and treating one as a photo puts a picture of the whole CV on
#: the person's profile.
PAGE_SCAN_MIN_WIDTH = 1000
PAGE_SCAN_MIN_HEIGHT = 1400
#: Guards against a decompression bomb: a photo is not 40 megapixels.
MAX_PIXELS = 40_000_000
#: The largest photo carried. It often rides back base64 encoded on a
#: response, which costs a third again on the wire.
MAX_PHOTO_BYTES = 4 * 1024 * 1024
#: Re-encoding quality for raw samples: visually indistinguishable at the
#: size a profile photo is displayed, about 22 KB for a 300x300 headshot.
JPEG_QUALITY = 90

#: Already an image file: hand it on rather than decode and re-encode it.
_MAGIC = ((b"\xff\xd8\xff", "image/jpeg"), (b"\x89PNG\r\n\x1a\n", "image/png"))

#: Samples per pixel, and the Pillow mode to read them as.
_COLOUR = {1: ("L", "L"), 3: ("RGB", "RGB")}


@dataclass(frozen=True)
class ExtractedPhoto:
    """A photo lifted out of a CV, ready to store."""

    content: bytes
    mime_type: str
    width: int
    height: int


@dataclass(frozen=True)
class _Candidate:
    """An image whose DIMENSIONS alone have been read. Decoding is the
    expensive half, and a CV routinely carries a letterhead banner, a logo or
    two and a page-sized background beside the one face."""

    page: int
    width: int
    height: int
    stream: PDFStream

    @property
    def rank(self) -> tuple[int, int]:
        """Earliest page first, then largest: a headshot sits on page one
        beside the name, and a logo that survives the aspect test is still
        the smaller of the two."""
        return (self.page, -(self.width * self.height))


def extract_profile_photo(raw: bytes, file_name: str) -> ExtractedPhoto | None:
    """The most photo-like image in a CV, or None.

    Never raises. A CV that cannot be read for a photo is still a perfectly
    good CV, and parsing must not fail over a nice-to-have.
    """
    suffix = Path(file_name).suffix.lower()
    try:
        if suffix == ".docx":
            return _from_docx(raw)
        if suffix == ".pdf" or raw[:5] == b"%PDF-":
            return _from_pdf(raw)
        logger.info("cv_photo: no photo reader for %s, skipped", suffix or "?")
        return None
    except Exception:  # noqa: BLE001 - a nice-to-have never fails the parse
        logger.warning("cv_photo: unreadable document %s", file_name, exc_info=True)
        return None


def looks_like_a_headshot(width: int, height: int) -> bool:
    """The size rule alone: big enough, not a page, portrait to mildly wide."""
    if width < MIN_SIDE_PX or height < MIN_SIDE_PX:
        return False
    if width * height > MAX_PIXELS:
        return False
    if width >= PAGE_SCAN_MIN_WIDTH and height >= PAGE_SCAN_MIN_HEIGHT:
        return False
    return MIN_ASPECT <= width / height <= MAX_ASPECT


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------


def _from_pdf(raw: bytes) -> ExtractedPhoto | None:
    candidates = [c for c in _image_xobjects(raw) if looks_like_a_headshot(c.width, c.height)]
    if not candidates:
        logger.info("cv_photo: no image in the PDF looks like a headshot")
        return None
    for candidate in sorted(candidates, key=lambda c: c.rank):
        photo = _decode(candidate)
        if photo is not None:
            return photo
    logger.info("cv_photo: %d candidate image(s), none decodable", len(candidates))
    return None


def _image_xobjects(raw: bytes) -> Iterator[_Candidate]:
    """Every image XObject in the document, with the page it is drawn on."""
    document = PDFDocument(PDFParser(BytesIO(raw)))
    for page_number, page in enumerate(PDFPage.create_pages(document), start=1):
        resources = resolve1(page.resources) or {}
        yield from _walk(resources, page_number, set())


def _walk(resources: dict, page: int, seen: set[int]) -> Iterator[_Candidate]:
    """Images in these resources, recursing into Form XObjects. The seen set
    is not an optimisation: a malformed document can point a form at itself."""
    xobjects = resolve1(resources.get("XObject")) or {}
    if not isinstance(xobjects, dict):
        return
    for ref in xobjects.values():
        marker = getattr(ref, "objid", None)
        stream = resolve1(ref)
        if not isinstance(stream, PDFStream):
            continue
        marker = marker if marker is not None else id(stream)
        if marker in seen:
            continue
        seen.add(marker)
        subtype = getattr(resolve1(stream.get("Subtype")), "name", None)
        if subtype == "Form":
            yield from _walk(resolve1(stream.get("Resources")) or {}, page, seen)
            continue
        if subtype != "Image":
            continue
        width = _as_int(stream.get("Width"))
        height = _as_int(stream.get("Height"))
        if width and height:
            yield _Candidate(page, width, height, stream)


def _decode(candidate: _Candidate) -> ExtractedPhoto | None:
    """Turn one already-sized image into bytes a browser can render."""
    stream, width, height = candidate.stream, candidate.width, candidate.height

    declared = _as_int(stream.get("Length"))
    if declared and declared > MAX_PHOTO_BYTES:
        return None

    # pdfminer applies the whole filter chain, predictors included, and leaves
    # an image codec's own output (a JPEG, say) alone.
    body = stream.get_data()
    if not body or len(body) > MAX_PHOTO_BYTES:
        return None

    for magic, mime in _MAGIC:
        if body.startswith(magic):
            return ExtractedPhoto(body, mime, width, height)

    if _as_int(stream.get("BitsPerComponent")) != 8:
        return None
    channels = _channels(stream.get("ColorSpace"))
    if channels is None or len(body) != width * height * channels:
        return None
    mode, target = _COLOUR[channels]
    return _encode(body, width, height, mode, target)


def _channels(colour_space: object) -> int | None:
    """Samples per pixel, for the colour spaces whose polarity is unambiguous.
    CMYK is deliberately absent."""
    space = resolve1(colour_space)
    name = getattr(space, "name", None)
    if name == "DeviceRGB":
        return 3
    if name == "DeviceGray":
        return 1
    if isinstance(space, list) and space:
        family = getattr(resolve1(space[0]), "name", None)
        if family == "ICCBased" and len(space) > 1:
            profile = resolve1(space[1])
            n = _as_int(profile.get("N")) if isinstance(profile, PDFStream) else None
            return n if n in _COLOUR else None
    return None


def _encode(samples: bytes, width: int, height: int, mode: str, target: str) -> ExtractedPhoto | None:
    """Raw samples out of the PDF, in as a JPEG."""
    buffer = BytesIO()
    with Image.frombytes(mode, (width, height), samples) as image:
        if image.mode != target:
            image = image.convert(target)
        image.save(buffer, format="JPEG", quality=JPEG_QUALITY)
    content = buffer.getvalue()
    if len(content) > MAX_PHOTO_BYTES:
        return None
    return ExtractedPhoto(content, "image/jpeg", width, height)


def _as_int(value: object) -> int | None:
    resolved = resolve1(value)
    return int(resolved) if isinstance(resolved, (int, float)) else None


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------


def _from_docx(raw: bytes) -> ExtractedPhoto | None:
    """A .docx is a zip, and its pictures are files in ``word/media``."""
    best: ExtractedPhoto | None = None
    with zipfile.ZipFile(BytesIO(raw)) as archive:
        for entry in archive.infolist():
            if not entry.filename.startswith("word/media/"):
                continue
            if entry.file_size > MAX_PHOTO_BYTES:
                continue
            photo = _docx_image(archive.read(entry.filename))
            if photo is None:
                continue
            if best is None or photo.width * photo.height > best.width * best.height:
                best = photo
    if best is None:
        logger.info("cv_photo: no image in the DOCX looks like a headshot")
    return best


def _docx_image(data: bytes) -> ExtractedPhoto | None:
    buffer = BytesIO()
    try:
        # Opening reads the header alone, so a picture is sized before any of
        # it is decoded and an oversized one is dropped without being read.
        with Image.open(BytesIO(data)) as image:
            width, height = image.size
            if not looks_like_a_headshot(width, height):
                return None
            for magic, mime in _MAGIC:
                if data.startswith(magic):
                    return ExtractedPhoto(data, mime, width, height)
            image.convert("RGB").save(buffer, format="JPEG", quality=JPEG_QUALITY)
    except Exception:  # noqa: BLE001
        logger.debug("cv_photo: unreadable DOCX image", exc_info=True)
        return None
    content = buffer.getvalue()
    if len(content) > MAX_PHOTO_BYTES:
        return None
    return ExtractedPhoto(content, "image/jpeg", width, height)
