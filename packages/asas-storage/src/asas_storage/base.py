"""Storage backend contract.

Keys are ``/``-separated relative paths (``orgs/{org}/photos/3/small.jpg``) —
exactly the strings the host stores in its DB. A key that is absolute or
escapes the root (``..``) is treated as nonexistent by reads and rejected by
writes: traversal safety lives here, not in callers.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Iterable, Iterator, Optional, Protocol, Tuple, runtime_checkable


@dataclass(frozen=True)
class FileStat:
    size: int
    content_type: Optional[str] = None


class RangeNotSatisfiable(ValueError):
    """``fetch_range`` was asked for bytes no object of this size has —
    ``start`` negative, past EOF, or past ``end``. Maps to HTTP 416 at the
    serving layer; a subclass of ``ValueError`` so pre-contract callers that
    caught broadly keep working."""


# ── Filename flattening for storage keys ────────────────────────────────────
#
# What the backends actually accept (checked 2026-09-28):
#
# - Amazon S3: a key is any UTF-8 sequence up to 1,024 BYTES, prefix and
#   delimiters included. "Safe" is ASCII alphanumerics plus ! - _ . * ' ( );
#   & $ @ = ; / : + , ? space and ASCII 0x00-0x1F / 0x7F "might require special
#   handling"; \ { } ^ % ` [ ] " < > ~ # | and 0x80-0xFF are "to avoid"; "." and
#   ".." segments misbehave in tools; CR/LF need XML entities in list/delete
#   bodies. https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-keys.html
# - Azure Blob: 1 to 1,024 CHARACTERS (the emulator: 256); control characters
#   0x00-0x1F and C1 (U+0080...) are not allowed; noncharacters such as U+FDD0-
#   U+FDEF, U+FFFE/U+FFFF and U+xFFFE/U+xFFFF, plus U+FFF0-U+FFFD, "may fail
#   UTF-8 or MBCS decoding"; private-use code points like U+E000 "cannot be
#   used"; no name or path segment should end with ".", "/" or "\".
#   https://learn.microsoft.com/en-us/rest/api/storageservices/naming-and-referencing-containers--blobs--and-metadata
# - Supabase Storage (an S3Storage target): isValidKey is
#   /^[A-Za-z0-9_/!.*'() &$=@;:+,?-]*$/, so it rejects EVERY non-ASCII key.
#   That, not U+202F specifically, is why the TEAMY-248 cutover saw 400s.
#   https://github.com/supabase/storage/blob/master/src/storage/limits.ts
#   That is why ASCII stays the DEFAULT (ascii_only=True): a live host is on
#   Supabase, and a Unicode default would turn its accented and Arabic uploads
#   into 400s that no local-storage test would catch. Hosts on AWS S3, Azure
#   Blob or LocalStorage opt in with ascii_only=False.
# - LocalStorage: one key segment is one file name, and NAME_MAX is 255 bytes
#   on ext4/XFS/APFS. put() writes ``.<segment>.<32 hex>.tmp`` first (38 more
#   bytes), and hosts compose ``<uuid>-<safe_filename>`` (37 more), leaving
#   180 bytes for the filename; the cap below keeps 20 bytes of that spare.
#
# So a kept character must be a letter, digit, or a combining mark attached to
# one (every category the stores reject or treat specially is outside that),
# plus the ASCII punctuation both S3 lists as safe or special and the old rule
# already shipped. Everything else becomes "_".

_SAFE_NAME_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 ._()-"
)
_SAFE_PUNCT = frozenset(" ._()-")

# The cap on the returned filename, in UTF-8 bytes: S3 and the local file
# system count bytes, and a byte cap is also a character cap for Azure. 160
# fits the LocalStorage budget above and keeps a typical
# ``orgs/<id>/documents/<uuid>-<name>`` key under the emulator's 256 characters.
MAX_FILENAME_BYTES = 160
# An extension longer than this is not treated as one when truncating.
_MAX_EXT_BYTES = 20

# Letters and marks that are Default_Ignorable_Code_Point (DerivedCoreProperties):
# rendered as nothing, so they could hide text inside a key. Hangul fillers are
# category Lo and would otherwise pass isalnum(); CGJ and the variation
# selectors are marks. Format characters (Cf) are flattened by category.
_INVISIBLE = frozenset(
    [0x115F, 0x1160, 0x3164, 0xFFA0, 0x034F]
    + list(range(0x180B, 0x1810))
    + list(range(0xFE00, 0xFE10))
    + list(range(0xE0100, 0xE01F0))
)


def _truncate_utf8(text: str, limit: int) -> str:
    """The longest prefix of ``text`` whose UTF-8 encoding fits ``limit``
    bytes, never splitting a code point."""
    out, used = [], 0
    for ch in text:
        size = len(ch.encode("utf-8"))
        if used + size > limit:
            break
        out.append(ch)
        used += size
    return "".join(out)


def safe_filename(
    name: str, *, ascii_only: bool = True, max_bytes: int = MAX_FILENAME_BYTES
) -> str:
    """Flatten a client filename into one safe storage-key segment.

    **Two charsets.** By default (``ascii_only=True``) only ASCII
    ``[A-Za-z0-9 ._()-]`` survives, which every backend accepts, Supabase
    Storage included. With ``ascii_only=False`` the rule below keeps letters in
    any script, so an Arabic name stays readable; use it on AWS S3, Azure Blob
    and ``LocalStorage``, never on Supabase, whose key validator rejects any
    non-ASCII character.

    The name is normalized to NFC first, so one visual name gives one key.
    With ``ascii_only=False``, kept: Unicode letters and digits (``str.isalnum()``), combining marks that
    follow one (Arabic harakat, Indic vowel signs), and the ASCII characters
    ``space . _ ( ) -``. Every other character becomes ``_``: all whitespace
    except a plain space (U+202F, U+00A0, tabs, line and paragraph
    separators), control characters, format characters (Cf, which includes
    the bidi marks, embeddings, overrides and isolates such as U+200F and
    U+202E, so ``invoice<U+202E>fdp.exe`` cannot display as ``invoiceexe.pdf``),
    path separators, symbols and emoji, private-use code points,
    noncharacters, and default-ignorable letters. Leading spaces and trailing
    spaces or dots are trimmed (Azure: no segment should end with a dot).

    The result is at most ``max_bytes`` UTF-8 bytes; a longer name loses the
    end of its stem, never its extension. It is never empty, ``.`` or ``..``
    (``file`` stands in) and never contains ``/``.

    NFC, the trim and the byte cap apply in both modes.

    Only the *key* is flattened; the display name stored on the row keeps the
    original. Uploaders prefix keys with a uuid, so flattening can't collide.
    """
    if max_bytes < 2 * _MAX_EXT_BYTES:
        raise ValueError(f"max_bytes must be at least {2 * _MAX_EXT_BYTES}")
    name = unicodedata.normalize("NFC", name or "")
    out = []
    attach = False  # the previous output char is a kept letter/digit/mark
    for ch in name:
        if ascii_only:
            keep = ch in _SAFE_NAME_CHARS
            attach = False
        elif ord(ch) in _INVISIBLE:
            keep = attach = False
        elif ch.isalnum():
            keep = attach = True
        elif unicodedata.category(ch).startswith("M"):
            keep = attach  # a mark on a letter; a stray one would sit on "_"
        else:
            keep = ch in _SAFE_PUNCT
            attach = False
        out.append(ch if keep else "_")
    cleaned = "".join(out).lstrip(" ").rstrip(" .")

    if len(cleaned.encode("utf-8")) > max_bytes:
        stem, dot, ext = cleaned.rpartition(".")
        suffix = dot + ext
        if not (dot and stem and len(suffix.encode("utf-8")) <= _MAX_EXT_BYTES):
            stem, suffix = cleaned, ""
        stem = _truncate_utf8(stem, max_bytes - len(suffix.encode("utf-8")))
        stem = stem.rstrip(" .")
        cleaned = (stem or "file") + suffix

    if not cleaned or cleaned in (".", ".."):
        # "." survives the charset filter, so a file literally named "." or
        # ".." would otherwise come back as a segment valid_key rejects.
        return "file"
    return cleaned


def clean_content_type(value: Optional[str]) -> Optional[str]:
    """Best-effort normalisation of a caller-supplied content type.

    The value travels verbatim into a backend request header, where hosts may
    source it from a client-controlled multipart part header: a CR/LF makes
    the SDK fail the request (azure-core after its full retry schedule,
    urllib3 with an unhandled ValueError), and a trailing space breaks Azure
    shared-key signing (403). Content type is best-effort by contract, so
    anything unheaderable is dropped rather than raised.
    """
    if not value:
        return None
    value = value.strip()
    if not value or any(not (" " <= ch <= "~") for ch in value):
        return None
    return value


def valid_key(key: str) -> bool:
    """A safe relative key: no absolute paths, no empty/`.`/`..` segments, no
    control characters (a NUL makes pathlib raise instead of the contract's
    bool/None/no-op answers). Backends share this so traversal behaves
    identically on disk and bucket."""
    if not key or key.startswith("/") or "\\" in key:
        return False
    if any(ch < " " or ch == "\x7f" for ch in key):
        return False
    return all(seg not in ("", ".", "..") for seg in key.split("/"))


@runtime_checkable
class Storage(Protocol):
    def put(self, key: str, data: bytes, content_type: Optional[str] = None) -> None:
        """Store ``data`` under ``key``, overwriting. Raises ValueError on a bad key."""
        ...

    def put_stream(
        self,
        key: str,
        chunks: Iterable[bytes],
        content_type: Optional[str] = None,
        *,
        overwrite: bool = True,
    ) -> int:
        """Store ``chunks`` under ``key`` WITHOUT holding the object in memory,
        and return the number of bytes written. For a large upload (a CV, a
        recording) that arrives as a stream: the path is chunk in, chunk out,
        so what the process holds tracks the chunk size and the concurrency,
        never the object. Each backend streams natively (a temp file and a
        rename, S3 multipart parts, Azure staged blocks); none buffers the
        iterator, which is what the SDKs do when handed one with no length.

        ``overwrite=False`` refuses a key that already holds an object with
        ``FileExistsError``, and the refusal is the backend's own atomic
        create-if-absent (``open("xb")``, ``If-None-Match: *``), never a
        look-then-write that a concurrent upload could slip between. A host
        whose keys carry a server-generated uuid uses it to turn a collision
        from silent cross-record data loss into an error.

        A stream that fails part-way leaves nothing under the key: no torn
        object, no half-written temp file, no abandoned multipart upload.
        Raises ValueError on a bad key."""
        ...

    def get(self, key: str) -> bytes:
        """Return the stored bytes. Raises FileNotFoundError when absent."""
        ...

    def stream(self, key: str) -> Iterator[bytes]:
        """Yield the stored bytes in chunks. Raises FileNotFoundError when absent."""
        ...

    def fetch(self, key: str) -> Tuple[FileStat, Iterator[bytes]]:
        """Stat + stream in one backend call (the serving hot path — on S3 a
        single GetObject instead of Head+Get). Raises FileNotFoundError when
        absent."""
        ...

    def fetch_range(
        self, key: str, start: int, end: int
    ) -> Tuple[FileStat, Iterator[bytes]]:
        """Stat + stream of the INCLUSIVE byte range ``[start, end]`` — HTTP
        Range semantics, added for video serving (Teamy TEAMY-679; a seek must
        not re-download from byte zero, and Safari refuses media URLs without
        206 support). ``FileStat.size`` is the TOTAL object size, so callers
        can build ``Content-Range: bytes start-end/total``; an ``end`` past
        EOF is clamped (also HTTP semantics). Raises FileNotFoundError when
        absent, :class:`RangeNotSatisfiable` when ``start`` is negative,
        beyond EOF, or greater than ``end``. Each backend serves the range
        natively (seek / ranged GetObject / offset+length download) — never by
        reading the whole object."""
        ...

    def exists(self, key: str) -> bool: ...

    def stat(self, key: str) -> Optional[FileStat]:
        """Size + best-effort content type, or ``None`` when absent."""
        ...

    def delete(self, key: str) -> None:
        """Remove the object; missing (or invalid) keys are a no-op."""
        ...

    def delete_prefix(self, prefix: str) -> int:
        """Remove every object under ``prefix`` (a directory-like subtree).
        Returns the number of objects removed."""
        ...

    def list(self, prefix: str) -> Iterator[str]:
        """Yield the keys under ``prefix`` (used by migration/export tooling)."""
        ...
