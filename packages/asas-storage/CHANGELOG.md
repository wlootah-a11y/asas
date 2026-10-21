# Changelog — `asas-storage`

Versions follow semver, and the git tag matches this file: `asas-storage/v0.15.1`.
Pre-1.0, a breaking change bumps the **minor**.

Release procedure and the historical tag mapping: [`RELEASING.md`](../../RELEASING.md).

## 0.15.1 (unreleased)

- Added `Storage.put_stream(key, chunks, content_type=None, *, overwrite=True) -> int`:
  store an object that arrives as a stream without holding it in memory, on
  every backend natively (a temp file and a rename on disk, >= 5 MiB multipart
  parts on S3, staged blocks on Azure, because `upload_blob(<iterator>)` drains
  the iterator whole). `overwrite=False` refuses an existing key with
  `FileExistsError` through the backend's own atomic create-if-absent
  (`open("xb")`, `If-None-Match: *`), and a stream that fails part-way leaves
  nothing under the key (no torn file, an aborted multipart upload, uncommitted
  blocks Azure discards). Additive: `put` is unchanged.
- **`safe_filename(name, ascii_only=False)` keeps letters in any script.** By
  default only ASCII `[A-Za-z0-9 ._()-]` survives, so an Arabic CV named
  `السيرة الذاتية.pdf` is stored as `______ _______.pdf` and operators cannot tell
  keys apart. Opting in normalizes to NFC (one visual name, one key), then keeps
  letters and digits (`str.isalnum()`), combining marks on them (Arabic harakat)
  and ASCII `space . _ ( ) -`. Everything else still becomes `_`: whitespace other
  than a plain space (U+202F, the TEAMY-248 case), control and format characters
  (including every bidi mark, embedding, override and isolate, so
  `invoice<U+202E>fdp.exe` cannot display as a `.pdf`), path separators, symbols
  and emoji, private-use code points, noncharacters and invisible letters. The S3,
  Azure Blob and Supabase rules behind each choice are cited in `base.py`.
- **Why ASCII stays the default.** Supabase Storage's key validator accepts ASCII
  only (`/^[A-Za-z0-9_/!.*'() &$=@;:+,?-]*$/`) and 400s on anything else, which is
  the real cause of the TEAMY-248 failures. A Unicode default would fail accented
  and Arabic uploads for a host on Supabase, and a suite on `LocalStorage` would
  not show it. AWS S3, Azure Blob and `LocalStorage` accept the opt-in.
- **Fix, both charsets: keys are capped at 160 UTF-8 bytes** (`max_bytes=`),
  keeping the extension, and never split a code point. `LocalStorage` writes one
  segment as one file name, limited to 255 bytes, and a `<uuid>-` prefix plus its
  temp name used to push a long name past that. Names are also NFC-normalized,
  and trailing dots and spaces are trimmed (Azure advises against a segment
  ending in a dot).
- **Existing keys are untouched.** Keys are stored strings, never recomputed.

## 0.15.0 — 2026-08-25

- Licensed under **Apache 2.0** (was proprietary/all-rights-reserved). `LICENSE` and `NOTICE` ship inside the wheel and the metadata carries `License-Expression: Apache-2.0` (Teamy TEAMY-797).
- Added `tests/test_host_contract.py`: `__all__` declared and resolving, contract names callable rather than shadowed by a submodule, module exports declared deliberately (Teamy TEAMY-798).

## Before 2026-08-25

Earlier releases were cut as **repo-wide** tags (`v0.1.0` … `v0.15.0`) under the
lockstep scheme in DR 0017, which decayed: from `v0.11.0` onward the repo tag no
longer matched any package's own version, so `asas-storage @ v0.15.0` did not install
`asas-storage` 0.15.0. `RELEASING.md` carries the full tag-to-version table for
decoding an old pin. Individual changes are in the git history.
