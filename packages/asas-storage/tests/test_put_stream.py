"""``put_stream``: an object stored from a stream, on every backend alike.

Runs on the shared parity fixture (local disk, S3 via moto, Azure Blob via
Azurite), so the four promises below hold identically: the bytes land whole
and in order, ``overwrite=False`` refuses a taken key atomically, a stream
that fails part-way leaves nothing under the key, and the count comes back.
"""

import uuid

import pytest

from asas_storage import Storage

from test_contract import store  # noqa: F401  (the parity fixture)

MIB = 1024 * 1024


def _chunks(payload: bytes, size: int):
    for offset in range(0, len(payload), size):
        yield payload[offset : offset + size]


def test_a_streamed_object_lands_whole_and_in_order(store):
    """Larger than one S3 part (5 MiB) and more than one Azure block, so
    every backend takes its multi-part path and the pieces must be joined
    in the order they arrived."""
    payload = bytes(range(256)) * (6 * MIB // 256) + b"tail"
    key = f"stream/{uuid.uuid4()}.bin"

    written = store.put_stream(key, _chunks(payload, MIB), content_type="application/octet-stream")

    assert written == len(payload)
    assert store.get(key) == payload
    stat = store.stat(key)
    assert stat.size == len(payload)
    assert stat.content_type == "application/octet-stream"


def test_a_small_object_takes_the_single_put_path(store):
    key = f"stream/{uuid.uuid4()}.txt"
    assert store.put_stream(key, iter([b"hello ", b"", b"world"]), content_type="text/plain") == 11
    assert store.get(key) == b"hello world"


def test_an_empty_stream_stores_an_empty_object(store):
    key = f"stream/{uuid.uuid4()}.empty"
    assert store.put_stream(key, iter([])) == 0
    assert store.exists(key) and store.get(key) == b""


def test_overwrite_replaces_and_refuse_refuses(store):
    key = f"stream/{uuid.uuid4()}.txt"
    store.put_stream(key, iter([b"first"]))

    assert store.put_stream(key, iter([b"second"])) == 6
    assert store.get(key) == b"second"

    with pytest.raises(FileExistsError):
        store.put_stream(key, iter([b"third"]), overwrite=False)
    assert store.get(key) == b"second", "a refused upload changes nothing"


def test_refuse_also_guards_the_multi_part_path(store):
    """The create-if-absent must hold on the commit of a staged upload, not
    only on the single PUT: that is where a large upload would otherwise
    overwrite a key it was told to leave alone."""
    key = f"stream/{uuid.uuid4()}.bin"
    store.put(key, b"existing")
    with pytest.raises(FileExistsError):
        store.put_stream(key, _chunks(b"x" * (6 * MIB), MIB), overwrite=False)
    assert store.get(key) == b"existing"


def test_refuse_then_store_when_the_key_is_free(store):
    key = f"stream/{uuid.uuid4()}.bin"
    assert store.put_stream(key, _chunks(b"y" * (6 * MIB), MIB), overwrite=False) == 6 * MIB
    assert store.stat(key).size == 6 * MIB


def test_a_stream_that_fails_part_way_leaves_nothing_under_the_key(store):
    key = f"stream/{uuid.uuid4()}.bin"

    def failing():
        yield b"a" * MIB
        yield b"b" * MIB
        raise RuntimeError("the upload broke")

    with pytest.raises(RuntimeError, match="the upload broke"):
        store.put_stream(key, failing(), overwrite=False)
    assert not store.exists(key)
    # And the key is still free to use afterwards.
    assert store.put_stream(key, iter([b"ok"]), overwrite=False) == 2


def test_a_failed_overwrite_keeps_the_old_object(store):
    key = f"stream/{uuid.uuid4()}.bin"
    store.put(key, b"old")

    def failing():
        yield b"new"
        raise RuntimeError("broke")

    with pytest.raises(RuntimeError):
        store.put_stream(key, failing())
    assert store.get(key) == b"old"


def test_bad_keys_and_hostile_content_types(store):
    with pytest.raises(ValueError):
        store.put_stream("../escape.bin", iter([b"x"]))
    key = f"stream/{uuid.uuid4()}.bin"
    store.put_stream(key, iter([b"x"]), content_type="text/plain\r\nx-injected: 1")
    assert store.stat(key).content_type != "text/plain\r\nx-injected: 1"


def test_put_stream_is_part_of_the_contract(store):
    assert isinstance(store, Storage)
    assert callable(getattr(type(store), "put_stream"))
