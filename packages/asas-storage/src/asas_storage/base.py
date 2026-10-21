"""Storage backend contract.

Keys are ``/``-separated relative paths (``orgs/{org}/photos/3/small.jpg``) —
exactly the strings the host stores in its DB. A key that is absolute or
escapes the root (``..``) is treated as nonexistent by reads and rejected by
writes: traversal safety lives here, not in callers.
"""

from __future__ import annotations

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


_SAFE_NAME_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 ._()-"
)


def safe_filename(name: str) -> str:
    """Flatten a client filename into the ASCII-safe charset for storage keys.

    Object stores reject keys real-world filenames produce — Supabase's S3
    layer 400s on macOS screenshot names, which carry a U+202F narrow
    no-break space before "AM/PM" (found the hard way during the TEAMY-248
    cutover). Only the *key* is flattened; the display name stored on the row
    keeps the original. Uploaders prefix keys with a uuid, so flattening
    can't collide."""
    cleaned = "".join(c if c in _SAFE_NAME_CHARS else "_" for c in name)
    cleaned = cleaned.strip()
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
