# asas-storage

A pluggable file/object storage seam: every stored byte goes through one process-wide
backend behind a traversal-safe `Storage` protocol. Three built-in backends —
`LocalStorage` (zero-config on-disk), `S3Storage` (any S3-compatible endpoint: AWS S3,
Supabase Storage, MinIO, R2; needs the `[s3]` extra) and `AzureBlobStorage` (needs the
`[azure]` extra) — with identical key semantics, so swapping backends never touches data.

**Azure Blob is a native backend, not an S3 alias.** Azure exposes no S3 API, so
`S3Storage` cannot reach it; the alternative would be an S3 translation gateway
(MinIO/S3Proxy), which adds a component to patch and forces a static key pair back into
the design. `AzureBlobStorage` speaks the Blob API directly, which is what lets the
preferred configuration hold **no credential at all**:

```python
AzureBlobStorage(container="uploads", account_url="https://acct.blob.core.windows.net")
```

authenticates via `DefaultAzureCredential` — managed identity in Azure, developer
credentials elsewhere. A `connection_string=` (account key) is accepted instead, for the
storage emulator and for deployments whose identity plane can't issue workload tokens.
Exactly one of the two is required: passing neither is a misconfiguration, and passing
both is ambiguous in a way that could leave an operator believing managed identity is in
use when a key is.

Keys are `/`-separated relative paths (the strings the host stores in its DB). Absolute
paths and `..`/empty segments are rejected identically on disk and bucket: traversal
safety lives in the library, not in callers.

Table-less **and** router-less variant of the Asas host contract: no session dependency,
no `seed`, no `migrate`, no `build_routers` — serving bytes over HTTP is host auth
territory. The host installs a backend factory at startup:

```python
import asas_storage
from asas_storage import LocalStorage, S3Storage

def _build() -> asas_storage.Storage:
    # read *your* settings; the library reads no configuration itself
    if settings.storage_backend == "s3":
        return S3Storage(bucket=..., access_key=..., secret_key=..., endpoint_url=...)
    return LocalStorage(settings.uploads_dir)

asas_storage.configure(_build)          # lazy: built on first storage() call
```

Call sites then use `storage()`:

```python
from asas_storage import storage, safe_filename

storage().put(f"orgs/{org}/documents/{uuid}-{safe_filename(name)}", data, content_type)
stat, chunks = storage().fetch(key)     # stat + stream in one backend call
storage().delete_prefix(f"orgs/{org}/photos/{member_id}")
```

`safe_filename` keeps only ASCII by default, the one charset every backend
accepts. On AWS S3, Azure Blob or `LocalStorage`, pass `ascii_only=False` to keep
letters and digits in any script, so Arabic names stay readable; never on
Supabase Storage, which rejects any non-ASCII key. Both modes cap the result at
160 UTF-8 bytes, extension kept.

Tests point the singleton at a tmp dir: `set_storage(LocalStorage(tmp_path))`
(`set_storage(None)` resets to lazy re-selection via the factory).

## Streaming uploads

`put` takes bytes. An object that arrives as a stream (a multipart upload part, a
recording) goes through `put_stream`, which never holds it whole:

```python
with upload.file as part:                     # anything that yields bytes
    size = storage().put_stream(key, iter(lambda: part.read(1 << 20), b""), content_type)
```

- **Each backend streams natively.** Disk writes a temp file and renames it; S3
  coalesces the chunks into >= 5 MiB multipart parts (one plain `PutObject` when the
  object never reaches one part); Azure stages blocks and commits the list. The Azure
  path is written the long way on purpose: `upload_blob(<iterator>)` drains the
  iterator to find its length (measured on a 5 MB object: a 9.1 MB heap against 2.2 MB
  for staged blocks, and `length` plus `max_concurrency=1` is worse at 10.1 MB).
- **`overwrite=False` refuses a key that is taken** with `FileExistsError`, through the
  backend's own atomic create-if-absent (`open("xb")`, `If-None-Match: *`), never a
  look-then-write. A host whose keys carry a server-generated uuid uses it to turn a
  collision from silent cross-record data loss into an error.
- **A stream that fails part-way leaves nothing under the key:** the temp file is
  unlinked, the multipart upload aborted, the staged blocks left uncommitted (invisible,
  and discarded by Azure after a week).

## Running the tests

The contract suite asserts backend *parity*: the same assertions run against local disk,
S3 (via moto) and Azure Blob (via Azurite). The Azure leg skips unless an emulator is
listening on `127.0.0.1:10000`, so start one first — with `--skipApiVersionCheck`, since
the SDK sends a newer API version than Azurite implements and every request 400s without
it (which presents as the whole leg quietly skipping, not as a failure):

```bash
docker run -d --rm --name azurite -p 10000:10000 mcr.microsoft.com/azure-storage/azurite:3.36.0 azurite-blob --blobHost 0.0.0.0 --skipApiVersionCheck
```

A full local run is `130 passed`; if you see skips, the emulator isn't up. In CI the
skip is promoted to a hard failure (`ASAS_REQUIRE_AZURE=1`), so the azure leg can
never vanish silently.

See the repo README for the full contract. Extracted from Teamy (storage seam TEAMY-248,
extraction epic TEAMY-466 / design record 0017).
