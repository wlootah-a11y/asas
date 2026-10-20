"""Deletion: two misses, never an incomplete walk, verified by key when inexact."""

import asyncio
from datetime import timedelta

import asas_sync
from tests.conftest import T0, FakeRemote, make_spec


def run(coro):
    return asyncio.run(coro)


def test_a_key_is_deleted_only_after_two_complete_walks_miss_it(factory, mirror):
    remote = FakeRemote(12, key_ordered=True)
    spec = make_spec(remote, mirror)
    run(asas_sync.run_pass(factory, spec, now=T0))
    first = run(asas_sync.reconcile(factory, spec, now=T0 + timedelta(hours=1)))
    assert first.complete and first.deleted == []  # the baseline
    del remote.rows[5]
    once = run(asas_sync.reconcile(factory, spec, now=T0 + timedelta(hours=2)))
    assert once.deleted == [] and "5" in mirror  # one miss never deletes
    twice = run(asas_sync.reconcile(factory, spec, now=T0 + timedelta(hours=3)))
    assert twice.deleted == ["5"] and "5" not in mirror and len(mirror) == 11


def test_an_incomplete_walk_condemns_nothing(factory, mirror):
    remote = FakeRemote(35, key_ordered=True)
    spec = make_spec(remote, mirror)
    run(asas_sync.run_pass(factory, spec, now=T0))
    run(asas_sync.reconcile(factory, spec, now=T0 + timedelta(hours=1)))
    run(asas_sync.reconcile(factory, spec, now=T0 + timedelta(hours=2)))
    capped = make_spec(remote, mirror, max_pages=1)
    result = run(asas_sync.reconcile(factory, capped, now=T0 + timedelta(hours=3)))
    assert not result.complete and result.deleted == [] and len(mirror) == 35
    with factory() as s:
        assert asas_sync.cursor_status(s, spec).reconciled_at == T0 + timedelta(hours=2)


def test_an_inexact_walk_deletes_only_what_the_remote_says_is_gone(factory, mirror):
    remote = FakeRemote(10, key_ordered=False)
    spec = make_spec(remote, mirror)
    run(asas_sync.run_pass(factory, spec, now=T0))
    run(asas_sync.reconcile(factory, spec, now=T0 + timedelta(hours=1)))
    for k in (2, 3, 4):
        del remote.rows[k]  # missed by the walks...
    remote.exists_answer = {"3": True, "4": None}  # ...but 3 is still there and 4 is unknown
    run(asas_sync.reconcile(factory, spec, now=T0 + timedelta(hours=2)))
    result = run(asas_sync.reconcile(factory, spec, now=T0 + timedelta(hours=3)))
    assert result.deleted == ["2"] and result.verified_present == 1 and result.undecided == 1
    assert "3" in mirror and "4" in mirror


def test_reconcile_walks_ask_for_the_reconcile_projection(factory, mirror):
    remote = FakeRemote(3, key_ordered=True)
    run(asas_sync.reconcile(factory, make_spec(remote, mirror), now=T0))
    assert {r["purpose"] for r in remote.requests} == {"reconcile"}
