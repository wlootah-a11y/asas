"""Backend contract parity (local disk vs S3 via moto vs Azure Blob via
Azurite), traversal safety, prefix directory-semantics, and key hygiene.
Ported from Teamy's tests/test_storage.py (TEAMY-248) with the extraction
(TEAMY-472); Azure Blob added with TEAMY-71."""

import os
import uuid

import pytest

from asas_storage import AzureBlobStorage, LocalStorage, S3Storage, safe_filename


def unicode_filename(name, **kw):
    """``safe_filename`` with the opt-in Unicode charset."""
    return safe_filename(name, ascii_only=False, **kw)


# Azurite's well-known emulator account — a published fixed credential, not a
# secret. CI runs Azurite as a service container; locally the azure leg skips
# unless you have one listening.
AZURITE_CONN = (
    "DefaultEndpointsProtocol=http;"
    "AccountName=devstoreaccount1;"
    "AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/"
    "K1SZFPTOtr/KBHBeksoGMGw==;"
    "BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1;"
)


@pytest.fixture(params=["local", "s3", "azure"])
def store(request, tmp_path):
    """The same contract assertions run against every backend."""
    if request.param == "local":
        yield LocalStorage(tmp_path)
        return
    if request.param == "s3":
        moto = pytest.importorskip("moto")
        import boto3

        with moto.mock_aws():
            boto3.client("s3", region_name="us-east-1").create_bucket(
                Bucket="asas-test"
            )
            yield S3Storage(
                bucket="asas-test",
                access_key="test",
                secret_key="test",
                region="us-east-1",
            )
        return

    pytest.importorskip("azure.storage.blob")
    from azure.core.exceptions import AzureError
    from azure.storage.blob import BlobServiceClient

    service = BlobServiceClient.from_connection_string(AZURITE_CONN)
    try:
        service.get_service_properties()
    except (AzureError, OSError) as exc:  # emulator not running
        if os.environ.get("ASAS_REQUIRE_AZURE"):
            pytest.fail(
                "ASAS_REQUIRE_AZURE is set but Azurite is unavailable at "
                f"127.0.0.1:10000 ({exc}) — the azure leg may not silently skip"
            )
        pytest.skip(f"Azurite unavailable at 127.0.0.1:10000 ({exc})")
    # A fresh container per test: Azure has no moto-style in-memory isolation,
    # so leaked blobs would otherwise cross-contaminate the assertions.
    name = f"asas-test-{uuid.uuid4().hex[:12]}"
    service.create_container(name)
    try:
        yield AzureBlobStorage(container=name, connection_string=AZURITE_CONN)
    finally:
        service.delete_container(name)


def test_put_get_roundtrip_and_overwrite(store):
    store.put("orgs/1/documents/2/a.txt", b"first", content_type="text/plain")
    assert store.get("orgs/1/documents/2/a.txt") == b"first"
    store.put("orgs/1/documents/2/a.txt", b"second")
    assert store.get("orgs/1/documents/2/a.txt") == b"second"


def test_get_missing_raises(store):
    with pytest.raises(FileNotFoundError):
        store.get("orgs/1/nope.bin")


def test_stream_yields_all_bytes(store):
    blob = b"x" * (200 * 1024) + b"tail"  # spans multiple chunks
    store.put("orgs/1/attachments/big.bin", blob)
    assert b"".join(store.stream("orgs/1/attachments/big.bin")) == blob
    with pytest.raises(FileNotFoundError):
        store.stream("orgs/1/attachments/missing.bin")


def test_exists_and_stat(store):
    assert not store.exists("orgs/1/photos/3/small.jpg")
    assert store.stat("orgs/1/photos/3/small.jpg") is None
    store.put("orgs/1/photos/3/small.jpg", b"jpegbytes", content_type="image/jpeg")
    assert store.exists("orgs/1/photos/3/small.jpg")
    stat = store.stat("orgs/1/photos/3/small.jpg")
    assert stat.size == len(b"jpegbytes")
    assert stat.content_type == "image/jpeg"


def test_delete_is_idempotent(store):
    store.put("orgs/1/documents/2/gone.txt", b"bye")
    store.delete("orgs/1/documents/2/gone.txt")
    assert not store.exists("orgs/1/documents/2/gone.txt")
    store.delete("orgs/1/documents/2/gone.txt")  # second delete is a no-op


def test_delete_prefix_uses_directory_semantics(store):
    """orgs/1/photos/1 must never swallow orgs/1/photos/12 (the classic
    string-prefix bug)."""
    for member in (1, 12):
        for name in ("small.jpg", "large.jpg"):
            store.put(f"orgs/1/photos/{member}/{name}", b"x")
    assert store.delete_prefix("orgs/1/photos/1") == 2
    assert not store.exists("orgs/1/photos/1/small.jpg")
    assert store.exists("orgs/1/photos/12/small.jpg")
    assert store.delete_prefix("orgs/1/photos/1") == 0  # idempotent


def test_list_returns_keys_under_prefix(store):
    store.put("orgs/1/documents/2/a.txt", b"a")
    store.put("orgs/1/documents/2/b.txt", b"b")
    store.put("orgs/2/documents/9/c.txt", b"c")
    assert sorted(store.list("orgs/1")) == [
        "orgs/1/documents/2/a.txt",
        "orgs/1/documents/2/b.txt",
    ]
    assert list(store.list("orgs/9")) == []


def test_traversal_keys_are_rejected(store):
    with pytest.raises(ValueError):
        store.put("../escape.txt", b"x")
    with pytest.raises(FileNotFoundError):
        store.get("orgs/../../etc/passwd")
    assert not store.exists("../escape.txt")
    assert store.stat("/absolute.txt") is None
    store.delete("../escape.txt")  # no-op, no error


def test_delete_of_directory_shaped_key_is_a_noop(store):
    """A key that names a prefix, not an object, is 'missing' on every
    backend — LocalStorage must not leak an IsADirectoryError where the
    bucket backends no-op."""
    store.put("orgs/1/documents/2/a.txt", b"x")
    store.delete("orgs/1/documents")  # no-op, no error
    assert store.exists("orgs/1/documents/2/a.txt")


def test_control_character_keys_are_rejected(store):
    """A NUL (or any control char) must get the contract's answers — bool /
    None / no-op / FileNotFoundError — not a pathlib ValueError."""
    for key in ("orgs/1/a\x00.txt", "orgs/1/a\n.txt"):
        with pytest.raises(ValueError):
            store.put(key, b"x")
        assert not store.exists(key)
        assert store.stat(key) is None
        store.delete(key)  # no-op, no error
        with pytest.raises(FileNotFoundError):
            store.get(key)


def test_hostile_content_type_is_dropped_not_raised(store):
    """content_type is best-effort by contract; a client-controlled value
    with CR/LF must be dropped, not raise out of the SDK's header
    validation as a 500 on the upload path."""
    key = "orgs/1/documents/2/h.bin"
    store.put(key, b"x", content_type="text/plain\r\nx-injected: 1")
    assert store.get(key) == b"x"
    stat = store.stat(key)
    assert stat is not None and stat.content_type != "text/plain\r\nx-injected: 1"


def test_fetch_returns_stat_and_bytes_in_one_call(store):
    """fetch() is the serving hot path: stat + stream from a single backend
    call — one GetObject on S3 instead of Head+Get."""
    store.put("orgs/1/photos/9/small.jpg", b"jpegdata", content_type="image/jpeg")
    stat, chunks = store.fetch("orgs/1/photos/9/small.jpg")
    assert stat.size == len(b"jpegdata")
    assert stat.content_type == "image/jpeg"
    assert b"".join(chunks) == b"jpegdata"
    with pytest.raises(FileNotFoundError):
        store.fetch("orgs/1/photos/9/missing.jpg")
    with pytest.raises(FileNotFoundError):
        store.fetch("../escape.jpg")


def test_safe_filename_flattens_exotic_characters():
    """Supabase's S3 layer 400s on keys with e.g. U+202F (macOS screenshot
    names) — safe_filename flattens anything outside the safe charset."""
    assert (
        unicode_filename("Screenshot 2025-08-11 at 4.26.18\u202fPM.png")
        == "Screenshot 2025-08-11 at 4.26.18_PM.png"
    )
    assert unicode_filename("Deep Research (v2).docx") == "Deep Research (v2).docx"
    # ASCII is the default (it is the one charset every backend accepts);
    # letters in any script are kept only when the host opts in.
    assert safe_filename("données_2025.csv") == "donn_es_2025.csv"
    assert unicode_filename("données_2025.csv") == "données_2025.csv"
    assert unicode_filename("\u202f") == "_"  # a lone exotic char still yields a key
    assert unicode_filename("") == "file"  # empty basename -> placeholder
    # "." survives the charset filter \u2014 a file named "." or ".." must not
    # come back as a segment valid_key rejects.
    assert unicode_filename(".") == "file"
    assert unicode_filename("..") == "file"
    assert unicode_filename(" .. ") == "file"


def test_arabic_filename_key_round_trips(store):
    """A key built by safe_filename from an Arabic name behaves like any other
    on every backend: put, get, stat, list, delete (opt-in Unicode keys, 0.15.1)."""
    key = f"orgs/1/documents/{uuid.uuid4()}-{unicode_filename('السيرة الذاتية.pdf')}"
    assert key.endswith("-السيرة الذاتية.pdf")
    store.put(key, b"%PDF", content_type="application/pdf")
    assert store.get(key) == b"%PDF"
    assert store.stat(key).size == 4
    assert list(store.list("orgs/1/documents")) == [key]
    store.delete(key)
    assert not store.exists(key)


def test_fetch_range_serves_http_range_semantics(store):
    """fetch_range (TEAMY-679): inclusive [start, end], total size on the
    stat, EOF clamping, and RangeNotSatisfiable — identical on every backend."""
    from asas_storage import RangeNotSatisfiable

    key = f"range/{uuid.uuid4()}.bin"
    payload = bytes(range(256)) * 4  # 1024 bytes, position-identifiable
    store.put(key, payload, content_type="application/octet-stream")

    # A middle slice, inclusive on both ends.
    stat, chunks = store.fetch_range(key, 100, 199)
    assert b"".join(chunks) == payload[100:200]
    assert stat.size == len(payload)  # TOTAL size — Content-Range needs it

    # First and last byte.
    _, chunks = store.fetch_range(key, 0, 0)
    assert b"".join(chunks) == payload[:1]
    _, chunks = store.fetch_range(key, len(payload) - 1, len(payload) - 1)
    assert b"".join(chunks) == payload[-1:]

    # end past EOF clamps (HTTP semantics).
    stat, chunks = store.fetch_range(key, 1000, 5000)
    assert b"".join(chunks) == payload[1000:]
    assert stat.size == len(payload)

    # Unsatisfiable starts → RangeNotSatisfiable (the serving layer's 416).
    # `start == size` is the boundary that matters: RFC 9110 §14.1.1 makes it
    # unsatisfiable, real Azure Blob 416s it, and Azurite answers an empty 206
    # instead — so every backend has to decide this itself rather than relay a
    # service's verdict (TEAMY-700).
    with pytest.raises(RangeNotSatisfiable):
        store.fetch_range(key, len(payload), len(payload) + 10)
    with pytest.raises(RangeNotSatisfiable):
        store.fetch_range(key, len(payload) + 100, len(payload) + 200)
    with pytest.raises(RangeNotSatisfiable):
        store.fetch_range(key, -1, 10)
    with pytest.raises(RangeNotSatisfiable):
        store.fetch_range(key, 50, 40)

    # A zero-length object has no satisfiable range at all — same boundary,
    # reached from the other side.
    empty = f"range/{uuid.uuid4()}.empty"
    store.put(empty, b"", content_type="application/octet-stream")
    with pytest.raises(RangeNotSatisfiable):
        store.fetch_range(empty, 0, 0)

    # Missing objects stay FileNotFoundError, not a range error.
    with pytest.raises(FileNotFoundError):
        store.fetch_range(f"range/{uuid.uuid4()}.bin", 0, 10)
