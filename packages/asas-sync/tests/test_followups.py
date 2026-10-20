"""What a pass tells its followers, records read by key, the upsert that never
goes backwards, waiting for a lease, and when a saved copy may be served."""

import asyncio
import time
from datetime import timedelta

import pytest
from sqlalchemy import Column, DateTime, MetaData, String, Table, select, update

import asas_sync
from asas_sync import SyncBusyError, SyncCursor
from tests.conftest import T0, FakeRemote, make_spec


def run(coro):
    return asyncio.run(coro)


class KeyedRemote(FakeRemote):
    async def fetch_keys(self, keys):
        self.requests.append(dict(keys=list(keys)))
        return [{"id": int(k), "updated": self.rows[int(k)]} for k in keys if int(k) in self.rows]


# ---- changed keys -------------------------------------------------------------------


def test_a_backfill_reports_everything_changed(factory, mirror):
    result = run(asas_sync.run_pass(factory, make_spec(FakeRemote(12, key_ordered=True), mirror), now=T0))
    assert result.changed_keys is None and result.changed == 12


def test_an_increment_reports_only_the_keys_that_changed(factory, mirror):
    remote = FakeRemote(25, key_ordered=True)
    spec = make_spec(remote, mirror)
    run(asas_sync.run_pass(factory, spec, now=T0))
    remote.rows[3] = T0 + timedelta(minutes=5)
    remote.rows[99] = T0 + timedelta(minutes=6)
    result = run(asas_sync.run_pass(factory, spec, now=T0 + timedelta(hours=1)))
    # The row AT the old watermark is read again, but it is no change.
    assert result.rows == 3
    assert sorted(result.changed_keys) == ["3", "99"] and result.changed == 2


def test_past_the_cap_it_reports_everything(factory, mirror):
    remote = FakeRemote(10, key_ordered=True)
    spec = make_spec(remote, mirror, changed_keys_cap=3)
    run(asas_sync.run_pass(factory, spec, now=T0))
    for k in (1, 2, 3, 4, 5):
        remote.rows[k] = T0 + timedelta(minutes=k)
    result = run(asas_sync.run_pass(factory, spec, now=T0 + timedelta(hours=1)))
    assert result.changed_keys is None and result.changed == 5


def test_a_pass_that_resumes_a_stopped_walk_reports_everything(factory, mirror):
    """The pages a stopped pass committed were never reported, so the pass
    that finishes its walk cannot name only what IT saw."""
    remote = FakeRemote(30, key_ordered=True)
    first = run(asas_sync.run_pass(factory, make_spec(remote, mirror, max_pages=1), now=T0))
    assert not first.complete
    resumed = run(asas_sync.run_pass(factory, make_spec(remote, mirror), now=T0 + timedelta(minutes=1)))
    assert resumed.complete and resumed.changed_keys is None


# ---- records by key -----------------------------------------------------------------


def test_refresh_keys_reads_and_upserts_by_key_without_moving_the_cursor(factory, mirror):
    remote = KeyedRemote(10, key_ordered=True)
    spec = make_spec(remote, mirror)
    run(asas_sync.run_pass(factory, spec, now=T0))
    with factory() as s:
        before = asas_sync.cursor_status(s, "people").watermark
    remote.rows[4] = T0 + timedelta(minutes=9)
    result = run(asas_sync.refresh_keys(factory, spec, ["4", "404", "4"]))
    assert result.asked == 2 and result.found == ["4"]
    assert mirror["4"] == T0 + timedelta(minutes=9)
    assert remote.requests[-1] == {"keys": ["4", "404"]}, "one request, no walk"
    with factory() as s:
        cursor = asas_sync.cursor_status(s, "people")
    assert cursor.watermark == before and cursor.lease_owner is None


def test_refresh_keys_needs_fetch_keys(factory, mirror):
    with pytest.raises(TypeError):
        run(asas_sync.refresh_keys(factory, make_spec(FakeRemote(3, key_ordered=True), mirror), ["1"]))


# ---- the upsert that never goes backwards ------------------------------------------


def test_upsert_newer_never_replaces_a_newer_row(factory, engine):
    meta = MetaData()
    rows_t = Table(
        "mirror_rows", meta,
        Column("id", String(16), primary_key=True),
        Column("title", String(64)),
        Column("updated", DateTime()),
    )
    meta.create_all(engine)

    def put(title, stamp):
        with factory() as s:
            asas_sync.upsert_newer(s, rows_t, [{"id": "1", "title": title, "updated": stamp}],
                                   key_columns=["id"], stamp_column="updated")
            s.commit()

    def held():
        with factory() as s:
            return s.execute(select(rows_t.c.title, rows_t.c.updated)).one()

    put("v2", T0 + timedelta(minutes=2))
    put("v1", T0 + timedelta(minutes=1))  # an older page arriving late
    assert held().title == "v2"
    put("v2 refilled", T0 + timedelta(minutes=2))  # equal stamps still rewrite
    assert held().title == "v2 refilled"
    put("v3", T0 + timedelta(minutes=3))
    assert held().title == "v3"


# ---- waiting for the lease ---------------------------------------------------------


def _hold_lease(factory, spec, *, for_s=3600):
    with factory() as s:
        asas_sync.engine._ensure_cursor(s, "", spec.resource)
        s.execute(update(SyncCursor).values(lease_owner="someone-else", lease_until=asas_sync.engine._now() + timedelta(seconds=for_s)))
        s.commit()


def test_without_a_wait_a_held_lease_is_busy_at_once(factory, mirror):
    spec = make_spec(FakeRemote(3, key_ordered=True), mirror)
    _hold_lease(factory, spec)
    started = time.monotonic()
    with pytest.raises(SyncBusyError):
        run(asas_sync.run_pass(factory, spec, now=T0))
    assert time.monotonic() - started < 0.5


def test_a_waiting_pass_runs_once_the_lease_is_released(factory, mirror):
    spec = make_spec(FakeRemote(3, key_ordered=True), mirror)
    _hold_lease(factory, spec)

    async def go():
        async def release_soon():
            await asyncio.sleep(0.15)
            with factory() as s:
                s.execute(update(SyncCursor).values(lease_owner=None, lease_until=None))
                s.commit()

        releaser = asyncio.create_task(release_soon())
        result = await asas_sync.run_pass(factory, spec, now=T0, wait_s=5, retry_s=0.05)
        await releaser
        return result

    assert run(go()).complete and len(mirror) == 3


def test_a_wait_that_runs_out_is_busy(factory, mirror):
    spec = make_spec(FakeRemote(3, key_ordered=True), mirror)
    _hold_lease(factory, spec)
    started = time.monotonic()
    with pytest.raises(SyncBusyError):
        run(asas_sync.run_pass(factory, spec, now=T0, wait_s=0.2, retry_s=0.05))
    assert 0.15 <= time.monotonic() - started < 2


# ---- when a saved copy may be served ----------------------------------------------


def test_a_saved_copy_is_current_only_while_the_mirror_vouches(factory, mirror):
    remote = FakeRemote(5, key_ordered=True)
    spec = make_spec(remote, mirror)
    stamp = T0 - timedelta(hours=1)
    assert not asas_sync.saved_copy_is_current(None, saved_stamp=stamp, mirrored_stamp=stamp)
    run(asas_sync.run_pass(factory, spec, now=T0))
    with factory() as s:
        cursor = asas_sync.cursor_status(s, "people")
    assert asas_sync.saved_copy_is_current(cursor, saved_stamp=stamp, mirrored_stamp=stamp)
    later = stamp + timedelta(minutes=1)
    assert not asas_sync.saved_copy_is_current(cursor, saved_stamp=stamp, mirrored_stamp=later), "changed upstream"
    cursor.last_error = "OracleUpstreamError: 502"
    assert not asas_sync.saved_copy_is_current(cursor, saved_stamp=stamp, mirrored_stamp=stamp), "a failed pass vouches for nothing"
