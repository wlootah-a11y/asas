"""On-disk backend — the zero-config default: files live under ``root/<key>``,
so a host adopting the seam over an existing uploads directory wakes up
unchanged."""

from __future__ import annotations

import mimetypes
import os
import shutil
import uuid
from pathlib import Path
from typing import BinaryIO, Iterable, Iterator, Optional, Tuple

from .base import FileStat, RangeNotSatisfiable, valid_key

_CHUNK = 64 * 1024
_PUT_ATTEMPTS = 8  # mkdir+write retries against a concurrent dir prune


class LocalStorage:
    def __init__(self, root: Path):
        self._root = root.resolve()

    def _full(self, key: str) -> Optional[Path]:
        """Resolve ``key`` under the root; ``None`` when invalid. ``valid_key``
        keeps key semantics identical across backends (``orgs//x`` or
        ``orgs/../x`` must not become a working alias on one backend only);
        the resolve+containment check stays as defense in depth."""
        if not valid_key(key):
            return None
        full = (self._root / key).resolve()
        if full == self._root or not str(full).startswith(str(self._root) + "/"):
            return None
        return full

    def put(self, key: str, data: bytes, content_type: Optional[str] = None) -> None:
        full = self._full(key)
        if full is None:
            raise ValueError(f"Invalid storage key: {key!r}")
        # Write-then-rename: S3/Azure overwrites are atomic, and an in-place
        # write_bytes is not — a concurrent fetch could stream a torn object.
        # The mkdir+write pair retries because a concurrent delete's
        # _prune_empty_dirs can remove the parent between the two steps.
        last_exc: OSError | None = None
        for _ in range(_PUT_ATTEMPTS):
            tmp = full.parent / f".{full.name}.{uuid.uuid4().hex}.tmp"
            try:
                full.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_bytes(data)
                os.replace(tmp, full)
                return
            except (FileNotFoundError, NotADirectoryError, FileExistsError) as exc:
                last_exc = exc
                tmp.unlink(missing_ok=True)
        raise last_exc  # a real filesystem problem, not the prune race

    def put_stream(
        self,
        key: str,
        chunks: Iterable[bytes],
        content_type: Optional[str] = None,
        *,
        overwrite: bool = True,
    ) -> int:
        full = self._full(key)
        if full is None:
            raise ValueError(f"Invalid storage key: {key!r}")
        # Overwriting: the same write-then-rename as put(), one chunk at a
        # time. Refusing: "xb" IS the collision guard, and it is atomic; an
        # exists() check would leave a race a streamed upload holds open for
        # the whole transfer. Either way a transfer that fails part-way
        # unlinks what it wrote, so no truncated object sits under a key
        # that now looks taken.
        last_exc: OSError | None = None
        for _ in range(_PUT_ATTEMPTS):
            target = full if not overwrite else full.parent / f".{full.name}.{uuid.uuid4().hex}.tmp"
            try:
                full.parent.mkdir(parents=True, exist_ok=True)
                handle = target.open("xb")
            except FileExistsError:
                if not overwrite:
                    raise
                continue  # a temp name collision: pick another
            except (FileNotFoundError, NotADirectoryError) as exc:
                last_exc = exc
                continue
            written = 0
            try:
                with handle:
                    for chunk in chunks:
                        handle.write(chunk)
                        written += len(chunk)
                if overwrite:
                    os.replace(target, full)
            except BaseException:
                target.unlink(missing_ok=True)
                raise
            return written
        raise last_exc  # a real filesystem problem, not the prune race

    def get(self, key: str) -> bytes:
        full = self._full(key)
        if full is None or not full.is_file():
            raise FileNotFoundError(key)
        return full.read_bytes()

    def stream(self, key: str) -> Iterator[bytes]:
        # Validate (and open) eagerly — contract parity with S3Storage — and
        # yield lazily from the already-open handle.
        return self._chunks(self._open(key))

    def fetch(self, key: str) -> Tuple[FileStat, Iterator[bytes]]:
        fh = self._open(key)
        # Size from the open handle: stat and stream then describe the same
        # object even when the key is overwritten before the iterator is
        # consumed — a stat-then-open pair could hand out a size that
        # disagrees with the bytes (a torn Content-Length at the serving layer).
        size = os.fstat(fh.fileno()).st_size
        stat = FileStat(size=size, content_type=mimetypes.guess_type(key)[0])
        return stat, self._chunks(fh)

    def _open(self, key: str) -> BinaryIO:
        full = self._full(key)
        if full is None:
            raise FileNotFoundError(key)
        try:
            fh = full.open("rb")
        except (FileNotFoundError, IsADirectoryError) as exc:
            raise FileNotFoundError(key) from exc
        return fh

    @staticmethod
    def _chunks(fh: BinaryIO) -> Iterator[bytes]:
        with fh:
            while chunk := fh.read(_CHUNK):
                yield chunk

    def fetch_range(
        self, key: str, start: int, end: int
    ) -> Tuple[FileStat, Iterator[bytes]]:
        fh = self._open(key)
        size = os.fstat(fh.fileno()).st_size
        if start < 0 or end < start or start >= size:
            fh.close()
            raise RangeNotSatisfiable(f"bytes={start}-{end} of {size}")
        end = min(end, size - 1)
        stat = FileStat(
            size=size, content_type=mimetypes.guess_type(key)[0]
        )
        return stat, self._range_chunks(fh, start, end - start + 1)

    @staticmethod
    def _range_chunks(fh: BinaryIO, offset: int, remaining: int) -> Iterator[bytes]:
        with fh:
            fh.seek(offset)
            while remaining > 0:
                chunk = fh.read(min(_CHUNK, remaining))
                if not chunk:  # truncated under our feet — stop cleanly
                    break
                remaining -= len(chunk)
                yield chunk

    def exists(self, key: str) -> bool:
        full = self._full(key)
        return full is not None and full.is_file()

    def stat(self, key: str) -> Optional[FileStat]:
        full = self._full(key)
        if full is None or not full.is_file():
            return None
        return FileStat(
            size=full.stat().st_size, content_type=mimetypes.guess_type(key)[0]
        )

    def delete(self, key: str) -> None:
        full = self._full(key)
        if full is None or full.is_dir():
            # Directory-shaped keys are a no-op like the bucket backends
            # (no such *object*), not an IsADirectoryError.
            return
        full.unlink(missing_ok=True)
        self._prune_empty_dirs(full.parent)

    def delete_prefix(self, prefix: str) -> int:
        full = self._full(prefix)
        if full is None or not full.is_dir():
            return 0
        count = sum(1 for p in full.rglob("*") if p.is_file())
        shutil.rmtree(full, ignore_errors=True)
        self._prune_empty_dirs(full.parent)
        return count

    def list(self, prefix: str) -> Iterator[str]:
        full = self._full(prefix)
        if full is None or not full.is_dir():
            return
        for path in sorted(full.rglob("*")):
            if path.is_file():
                yield path.relative_to(self._root).as_posix()

    def _prune_empty_dirs(self, directory: Path) -> None:
        """Best-effort: drop now-empty parent dirs so the local tree doesn't
        accumulate husks. Never removes the root."""
        while directory != self._root and directory.is_dir():
            try:
                directory.rmdir()  # only succeeds when empty
            except OSError:
                return
            directory = directory.parent
