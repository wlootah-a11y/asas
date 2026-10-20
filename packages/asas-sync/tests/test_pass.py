"""Incremental passes: backfill, increments, the watermark rules, the walk orders."""

import asyncio
from datetime import timedelta

import pytest

import asas_sync
from asas_sync import SyncBusyError, SyncStuckError
from tests.conftest import T0, FakeRemote, make_spec


def run(coro):
    return asyncio.run(coro)


def test_a_backfill_reads_everything_and_sets_the_cursor(factory, mirror):
    remote = FakeRemote(25, key_ordered=True)
    result = run(asas_sync.run_pass(factory, make_spec(remote, mirror), now=T0))
    assert result.complete and result.rows == 25 and len(mirror) == 25
    with factory() as s:
        cursor = asas_sync.cursor_status(s, "people")
    assert cursor.backfilled_at == T0 and cursor.last_complete and cursor.lease_owner is None
    assert cursor.watermark == max(remote.rows.values())


def test_the_next_pass_reads_only_what_changed(factory, mirror):
    remote = FakeRemote(25, key_ordered=True)
    spec = make_spec(remote, mirror)
    run(asas_sync.run_pass(factory, spec, now=T0))
    remote.rows[3] = T0 + timedelta(minutes=5)
    remote.rows[99] = T0 + timedelta(minutes=6)
    result = run(asas_sync.run_pass(factory, spec, now=T0 + timedelta(hours=1)))
    # the row at the old watermark is read again (>=), plus the two changes
    assert result.rows == 3 and mirror["3"] == T0 + timedelta(minutes=5) and "99" in mirror


def test_a_row_changed_during_the_walk_is_not_lost(factory, mirror):
    """The watermark never passes the walk's own start: a row the walk had
    already passed, modified mid-walk, is read by the next pass."""
    remote = FakeRemote(30, key_ordered=True)

    def modify_key_1(remote, request_number):
        if request_number == 2:  # page 1 (keys 1-10) is already done
            remote.rows[1] = T0 + timedelta(seconds=30)  # after the pass started at T0

    remote.during_fetch = modify_key_1
    spec = make_spec(remote, mirror)
    first = run(asas_sync.run_pass(factory, spec, now=T0))
    assert first.watermark <= T0
    remote.during_fetch = None
    run(asas_sync.run_pass(factory, spec, now=T0 + timedelta(minutes=5)))
    assert mirror["1"] == T0 + timedelta(seconds=30)


def test_stamp_order_with_unstable_ties_misses_nothing(factory, mirror):
    """Ties shuffled on every request, in blocks smaller than a page: the walk
    re-anchors on the greatest stamp with offset 0, so no row is skipped."""
    remote = FakeRemote(60, key_ordered=False, shuffle_ties=True)
    for k in remote.rows:  # blocks of four rows sharing one stamp
        remote.rows[k] = T0 - timedelta(hours=2) + timedelta(minutes=(k - 1) // 4)
    run(asas_sync.run_pass(factory, make_spec(remote, mirror), now=T0))
    assert len(mirror) == 60
    assert all(r["offset"] == 0 for r in remote.requests)


def test_the_offset_ceiling_re_anchors_instead_of_stopping(factory, mirror):
    remote = FakeRemote(45, key_ordered=False, ceiling=20)
    result = run(asas_sync.run_pass(factory, make_spec(remote, mirror, offset_ceiling=20), now=T0))
    assert result.complete and len(mirror) == 45


def test_a_key_filter_that_does_not_filter_is_stuck_not_a_loop(factory, mirror):
    remote = FakeRemote(25, key_ordered=True, ignore_after_key=True)
    with pytest.raises(SyncStuckError):
        run(asas_sync.run_pass(factory, make_spec(remote, mirror), now=T0))
    with factory() as s:
        cursor = asas_sync.cursor_status(s, "people")
    assert "SyncStuckError" in cursor.last_error and cursor.lease_owner is None


def test_a_capped_pass_resumes_where_it_stopped(factory, mirror):
    remote = FakeRemote(35, key_ordered=True)
    spec = make_spec(remote, mirror, max_pages=2)
    first = run(asas_sync.run_pass(factory, spec, now=T0))
    assert not first.complete and len(mirror) == 20 and first.watermark is None
    second = run(asas_sync.run_pass(factory, spec, now=T0 + timedelta(minutes=1)))
    assert second.complete and len(mirror) == 35
    assert remote.requests[2]["after_key"] == "20"  # resumed, not restarted


def test_one_pass_at_a_time_and_a_dead_holder_expires(factory, mirror):
    from asas_sync.engine import _claim

    remote = FakeRemote(5, key_ordered=True)
    spec = make_spec(remote, mirror, lease_s=3600)
    with factory() as s:
        assert _claim(s, "", spec, "someone-else")
        s.commit()
    with pytest.raises(SyncBusyError):
        run(asas_sync.run_pass(factory, spec, now=T0))
    expired = make_spec(remote, mirror, lease_s=-1)
    with factory() as s:
        assert _claim(s, "", expired, "someone-else")  # re-take with an already-expired lease
        s.commit()
    assert run(asas_sync.run_pass(factory, spec, now=T0)).complete


def test_organisations_have_their_own_cursors(factory, mirror):
    remote = FakeRemote(5, key_ordered=True)
    spec = make_spec(remote, mirror)
    run(asas_sync.run_pass(factory, spec, org_id=1, now=T0))
    run(asas_sync.run_pass(factory, spec, org_id="acme", now=T0))
    with factory() as s:
        assert asas_sync.cursor_status(s, spec, org_id="1").last_complete
        assert asas_sync.cursor_status(s, spec, org_id="acme").last_complete
        assert asas_sync.cursor_status(s, spec) is None
