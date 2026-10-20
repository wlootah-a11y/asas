"""Mirror a remote paginated collection into the host's tables, incrementally.

Extracted from a production mirror of an HR system's collections, where every
rule below was learned from a measured incident on a live system. The remote is behind :class:`RemoteCollection`; the rows are
the host's (``upsert`` and ``on_deleted``); this package owns only the cursor
and the seen-marks.

**Walk order, because offset paging over ties loses rows.** Many APIs do not
guarantee the order among rows that share a timestamp between two requests,
so a walk that pages by offset across such a block skips some rows and
repeats others. So:

* ``key_ordered=True`` (the remote can filter ``stamp >= since AND key >
  after`` and order by key): the walk re-anchors on the last key after every
  page with offset 0. Nothing depends on the order among ties.
* otherwise the walk is in stamp order and re-anchors on the page's greatest
  stamp with offset 0, using an offset only INSIDE a block of rows sharing one
  stamp that is longer than a page. Rows at the anchor are read twice; the
  upsert must be idempotent (it always must: delivery is at-least-once).

**The offset ceiling.** Some APIs stop serving at a fixed offset and report
``has_more=False`` on the last page they will serve even though rows remain
(Oracle: 10,000). ``offset_ceiling`` makes "no more" at the ceiling mean
"re-anchor", not "done".

**The watermark never passes the walk's own start** (less ``clock_skew_s``),
and never passes the greatest stamp actually read: a row modified WHILE the
walk ran carries a stamp the walk may already be past, and a watermark set to
"now" at the end would never see it again. Stamp-ordered walks advance it per
committed page; key-ordered walks keep the last key per page (to resume a
capped pass) and set the watermark when the walk completes.

**Deletion needs two misses.** A reconcile walk marks every key it meets. A
key is gone only when TWO consecutive complete reconcile walks failed to meet
it; the first reconcile only lays the baseline, and an incomplete walk marks
nothing, because a walk must not condemn what it never reached. A stamp-ordered
walk is not exact (see above), so there a twice-missed key is read by key with
``exists`` and only an explicit "gone" deletes it.

**One pass per collection at a time,** by a lease on the cursor row taken with
compare-and-set (portable; no advisory lock), renewed per page and expiring on
its own if the holder dies. A pass that must not be dropped (one triggered by a
change notification) passes ``wait_s`` and retries the claim between short
transactions, so it holds no database connection while it waits.

**Follow-ups refresh what changed.** A pass reports the keys it met with a
stamp AFTER its starting watermark (``PassResult.changed_keys``), so work that
follows a pass (re-reading the records the mirror feeds) can touch only those.
``None`` means "treat everything as changed": a walk from nothing, a full walk,
a walk that RESUMED a stopped pass (its earlier pages were never reported), or
more changes than ``SyncSpec.changed_keys_cap``.

**A record named by a notification is read by key** (:func:`refresh_keys`):
no walk and no lease, so it is safe to race a walk ONLY because the host's
upsert never goes backwards (an older version never replaces a newer row).
:func:`upsert_newer` is that upsert for a plain table.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional, Protocol, Sequence

from sqlalchemy import and_, delete, or_, select, update
from sqlmodel import Session

from .models import SyncCursor, SyncSeen

log = logging.getLogger(__name__)


# ---- the host's side ----------------------------------------------------------------


@dataclass(frozen=True)
class RemotePage:
    rows: Sequence[Any]
    has_more: bool


class RemoteCollection(Protocol):
    """The remote API, as this package needs it.

    ``fetch`` returns rows with ``stamp >= since`` (all rows when ``since`` is
    None), and with ``key > after_key`` when the spec is key-ordered and
    ``after_key`` is given, ordered by key (key-ordered) or by stamp (not),
    starting at ``offset``. ``purpose`` is ``"sync"`` or ``"reconcile"``, so a
    host can ask for only the key and stamp on a reconcile walk.

    ``exists`` answers True (present), False (gone, e.g. a 404) or None
    (cannot say right now); only False ever deletes.
    """

    async def fetch(
        self, *, since: Optional[datetime], after_key: Optional[str], offset: int, limit: int, purpose: str
    ) -> RemotePage: ...

    async def exists(self, key: str) -> Optional[bool]: ...

    # OPTIONAL, needed only by :func:`refresh_keys`:
    #
    #   async def fetch_keys(self, keys: Sequence[str]) -> Sequence[Any]
    #
    # the rows for these keys (a missing key is simply absent: deletion is the
    # reconcile's to decide).


@dataclass(frozen=True)
class SyncSpec:
    resource: str
    collection: RemoteCollection
    key: Callable[[Any], Any]
    stamp: Callable[[Any], Optional[datetime]]
    upsert: Callable[[Session, list[Any]], None]
    on_deleted: Optional[Callable[[Session, list[str]], None]] = None
    key_ordered: bool = False
    page_size: int = 500
    offset_ceiling: Optional[int] = None
    max_pages: int = 10_000
    clock_skew_s: float = 60.0
    lease_s: float = 1800.0
    #: Past this many changed keys a pass reports ``changed_keys=None``.
    changed_keys_cap: int = 2000
    #: How keys compare, for the key-ordered walk's progress check. The default
    #: compares numerically when both keys are digits, as text otherwise.
    key_sort: Callable[[str], Any] = field(default=lambda k: (0, int(k), "") if k.isdigit() else (1, 0, k))


class SyncBusyError(RuntimeError):
    """Another pass holds this collection's lease."""


class SyncStuckError(RuntimeError):
    """The remote kept answering without the walk making progress."""


@dataclass
class PassResult:
    resource: str
    rows: int = 0
    pages: int = 0
    complete: bool = False
    watermark: Optional[datetime] = None
    duration_ms: int = 0
    #: Rows met with a stamp after the pass's starting watermark.
    changed: int = 0
    #: Those rows' keys, or ``None`` for "treat everything as changed" (see
    #: the module doc). An empty list means nothing changed.
    changed_keys: Optional[list[str]] = None


@dataclass
class RefreshResult:
    resource: str
    asked: int = 0
    #: The keys the remote returned, and therefore upserted.
    found: list[str] = field(default_factory=list)


@dataclass
class ReconcileResult:
    resource: str
    walked: int = 0
    complete: bool = False
    deleted: list[str] = field(default_factory=list)
    verified_present: int = 0
    undecided: int = 0


# ---- helpers ------------------------------------------------------------------------


def _utc(moment: Optional[datetime]) -> Optional[datetime]:
    if moment is not None and moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
    return moment


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _key_text(spec: SyncSpec, row: Any) -> Optional[str]:
    value = spec.key(row)
    return None if value is None or value == "" else str(value)


async def _db(session_factory: Callable[[], Session], fn: Callable[[Session], Any]) -> Any:
    """One short transaction, off the event loop."""

    def run() -> Any:
        with session_factory() as session:
            out = fn(session)
            session.commit()
            return out

    return await asyncio.to_thread(run)


def _ensure_cursor(session: Session, org: str, resource: str) -> None:
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
    else:
        from sqlalchemy.dialects.sqlite import insert as dialect_insert
    session.execute(
        dialect_insert(SyncCursor)
        .values(org_id=org, resource=resource, last_complete=False)
        .on_conflict_do_nothing(index_elements=["org_id", "resource"])
    )


def _cursor(session: Session, org: str, resource: str) -> SyncCursor:
    return session.execute(
        select(SyncCursor).where(SyncCursor.org_id == org, SyncCursor.resource == resource)
    ).scalar_one()


def _claim(session: Session, org: str, spec: SyncSpec, owner: str) -> bool:
    _ensure_cursor(session, org, spec.resource)
    now = _now()
    moved = session.execute(
        update(SyncCursor)
        .where(
            SyncCursor.org_id == org,
            SyncCursor.resource == spec.resource,
            or_(SyncCursor.lease_until.is_(None), SyncCursor.lease_until < now, SyncCursor.lease_owner == owner),
        )
        .values(lease_owner=owner, lease_until=now + timedelta(seconds=spec.lease_s))
    )
    return moved.rowcount == 1


async def _claim_or_wait(
    session_factory: Callable[[], Session],
    org: str,
    spec: SyncSpec,
    owner: str,
    *,
    wait_s: float,
    retry_s: float,
) -> None:
    """Take the lease, retrying for up to ``wait_s``. Each attempt is its own
    short transaction and the wait sleeps outside any, so a waiting pass holds
    no database connection (a pool of a few would otherwise be drained by
    passes waiting on one another)."""
    deadline = time.monotonic() + max(0.0, wait_s)
    while True:
        if await _db(session_factory, lambda s: _claim(s, org, spec, owner)):
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SyncBusyError(f"{spec.resource} is already being walked for {org or 'the platform'!r}")
        await asyncio.sleep(min(max(retry_s, 0.01), remaining))


def _release(session: Session, org: str, resource: str, owner: str) -> None:
    session.execute(
        update(SyncCursor)
        .where(SyncCursor.org_id == org, SyncCursor.resource == resource, SyncCursor.lease_owner == owner)
        .values(lease_owner=None, lease_until=None)
    )


def _mark_seen(session: Session, org: str, resource: str, keys: list[str], at: datetime) -> None:
    if not keys:
        return
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
    else:
        from sqlalchemy.dialects.sqlite import insert as dialect_insert
    stmt = dialect_insert(SyncSeen).values(
        [{"org_id": org, "resource": resource, "key": k, "seen_at": at} for k in dict.fromkeys(keys)]
    )
    session.execute(stmt.on_conflict_do_update(index_elements=["org_id", "resource", "key"], set_={"seen_at": at}))


# ---- the walk -----------------------------------------------------------------------


async def _walk(
    spec: SyncSpec,
    *,
    since: Optional[datetime],
    after_key: Optional[str],
    purpose: str,
    on_page: Callable[[list[Any], Optional[datetime], Optional[str]], Any],
) -> tuple[bool, int, int]:
    """Walk from ``since`` (and ``after_key``), awaiting ``on_page(rows,
    page_max_stamp, last_key)`` for each page; ``on_page`` is where the page
    commits, so a failure stops the walk with the cursor at the last commit.
    Returns ``(complete, pages, rows)``."""
    limit = max(1, spec.page_size)
    anchor = since
    last_key = after_key
    offset = 0
    pages = rows_total = 0
    while pages < spec.max_pages:
        page = await spec.collection.fetch(
            since=anchor if not spec.key_ordered else since,
            after_key=last_key if spec.key_ordered else None,
            offset=offset,
            limit=limit,
            purpose=purpose,
        )
        rows = [r for r in page.rows if _key_text(spec, r) is not None]
        page_max: Optional[datetime] = None
        for r in rows:
            stamp = _utc(spec.stamp(r))
            if stamp is not None and (page_max is None or stamp > page_max):
                page_max = stamp
        pages += 1
        rows_total += len(rows)
        if spec.key_ordered and rows:
            keys = [_key_text(spec, r) for r in rows]
            newest = max(keys, key=spec.key_sort)
            if last_key is not None and spec.key_sort(newest) <= spec.key_sort(last_key):
                raise SyncStuckError(f"{spec.resource}: the key did not advance past {last_key!r}")
            last_key = newest
        await on_page(rows, page_max, last_key)
        if not rows:
            return True, pages, rows_total
        if spec.key_ordered:
            if not page.has_more:
                return True, pages, rows_total
            continue
        next_offset = offset + len(rows)
        at_ceiling = spec.offset_ceiling is not None and next_offset >= spec.offset_ceiling
        # "No more" is the end EXCEPT at the ceiling, where the remote says it
        # about the last page it is willing to serve.
        if not page.has_more and not at_ceiling:
            return True, pages, rows_total
        if page_max is not None and (anchor is None or page_max > anchor):
            anchor, offset = page_max, 0
            continue
        if spec.offset_ceiling is not None and next_offset + limit > spec.offset_ceiling:
            raise SyncStuckError(
                f"{spec.resource}: more rows share the stamp {anchor} than the offset ceiling allows"
            )
        offset = next_offset
    log.warning("asas-sync %s: walk capped at %d pages; the next pass resumes", spec.resource, spec.max_pages)
    return False, pages, rows_total


# ---- the passes ---------------------------------------------------------------------


async def run_pass(
    session_factory: Callable[[], Session],
    spec: SyncSpec,
    *,
    org_id: Any = "",
    full: bool = False,
    now: Optional[datetime] = None,
    wait_s: float = 0.0,
    retry_s: float = 15.0,
) -> PassResult:
    """One incremental pass (``full=True`` walks everything). Raises
    :class:`SyncBusyError` when another pass holds the lease (after waiting up
    to ``wait_s`` for it, retrying every ``retry_s``); any other failure is
    recorded on the cursor and re-raised."""
    org = "" if org_id is None else str(org_id)
    owner = uuid.uuid4().hex
    await _claim_or_wait(session_factory, org, spec, owner, wait_s=wait_s, retry_s=retry_s)
    started_clock = time.monotonic()
    started = _utc(now) or _now()
    ceiling = started - timedelta(seconds=spec.clock_skew_s)
    result = PassResult(resource=spec.resource)
    try:
        def read(session: Session) -> tuple[Optional[datetime], Optional[str], bool]:
            row = _cursor(session, org, spec.resource)
            return row.watermark, row.last_key, row.last_complete

        watermark, last_key, last_complete = await _db(session_factory, read)
        since = None if full else watermark
        resume = last_key if (spec.key_ordered and not full and not last_complete) else None
        highest: list[Optional[datetime]] = [watermark if not full else None]
        # Changes are reported against the starting watermark, strictly after
        # it: the row AT the watermark is re-read every pass and is no change.
        changed: list[str] = []
        overflow = [since is None or resume is not None]

        async def on_page(rows: list[Any], page_max: Optional[datetime], last_key: Optional[str]) -> None:
            keys = [_key_text(spec, r) for r in rows]
            for r, k in zip(rows, keys):
                stamp = _utc(spec.stamp(r))
                if k is not None and (since is None or (stamp is not None and stamp > since)):
                    result.changed += 1
                    if not overflow[0]:
                        changed.append(k)
                        if len(changed) > spec.changed_keys_cap:
                            overflow[0] = True

            def commit(session: Session) -> None:
                if rows:
                    spec.upsert(session, rows)
                    _mark_seen(session, org, spec.resource, keys, started)
                values: dict[str, Any] = {"lease_until": _now() + timedelta(seconds=spec.lease_s)}
                if spec.key_ordered:
                    values["last_key"] = last_key
                elif page_max is not None:
                    values["watermark"] = min(page_max, ceiling)
                session.execute(
                    update(SyncCursor)
                    .where(SyncCursor.org_id == org, SyncCursor.resource == spec.resource)
                    .values(**values)
                )

            await _db(session_factory, commit)
            if page_max is not None and (highest[0] is None or page_max > highest[0]):
                highest[0] = page_max

        complete, result.pages, result.rows = await _walk(
            spec, since=since, after_key=resume, purpose="sync", on_page=on_page
        )
        result.complete = complete
        result.changed_keys = None if overflow[0] else list(dict.fromkeys(changed))
        final: dict[str, Any] = {
            "last_complete": complete,
            "last_run_at": started,
            "last_error": None,
            "last_run_ms": int((time.monotonic() - started_clock) * 1000),
        }
        if complete:
            final["last_key"] = None
            if highest[0] is not None:
                final["watermark"] = min(highest[0], ceiling)
            if since is None:
                final["backfilled_at"] = started
        result.duration_ms = final["last_run_ms"]

        def finish(session: Session) -> Optional[datetime]:
            session.execute(
                update(SyncCursor)
                .where(SyncCursor.org_id == org, SyncCursor.resource == spec.resource)
                .values(**final)
            )
            return _cursor(session, org, spec.resource).watermark

        result.watermark = await _db(session_factory, finish)
        return result
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"[:500]
        await _db(
            session_factory,
            lambda s: s.execute(
                update(SyncCursor)
                .where(SyncCursor.org_id == org, SyncCursor.resource == spec.resource)
                .values(last_error=message, last_run_at=started)
            ),
        )
        raise
    finally:
        await _db(session_factory, lambda s: _release(s, org, spec.resource, owner))


async def reconcile(
    session_factory: Callable[[], Session],
    spec: SyncSpec,
    *,
    org_id: Any = "",
    now: Optional[datetime] = None,
    wait_s: float = 0.0,
    retry_s: float = 15.0,
) -> ReconcileResult:
    """Walk the whole collection marking every key met, then delete what two
    consecutive complete walks both missed (see the module doc)."""
    org = "" if org_id is None else str(org_id)
    owner = uuid.uuid4().hex
    await _claim_or_wait(session_factory, org, spec, owner, wait_s=wait_s, retry_s=retry_s)
    started = _utc(now) or _now()
    result = ReconcileResult(resource=spec.resource)
    try:
        previous = await _db(session_factory, lambda s: _cursor(s, org, spec.resource).reconciled_at)

        async def on_page(rows: list[Any], page_max: Optional[datetime], last_key: Optional[str]) -> None:
            keys = [_key_text(spec, r) for r in rows]
            await _db(session_factory, lambda s: _mark_seen(s, org, spec.resource, keys, started))

        complete, _, result.walked = await _walk(
            spec, since=None, after_key=None, purpose="reconcile", on_page=on_page
        )
        result.complete = complete
        if not complete:
            return result  # an incomplete walk must not condemn what it never reached
        if previous is not None:
            unseen = await _db(
                session_factory,
                lambda s: list(
                    s.execute(
                        select(SyncSeen.key).where(
                            SyncSeen.org_id == org,
                            SyncSeen.resource == spec.resource,
                            SyncSeen.seen_at < previous,
                        )
                    ).scalars()
                ),
            )
            gone: list[str] = []
            if spec.key_ordered:
                gone = unseen  # an exact walk: what two walks missed is gone
            else:
                for key in unseen:
                    try:
                        present = await spec.collection.exists(key)
                    except Exception:  # noqa: BLE001 - unknown today, asked again next time
                        present = None
                    if present is False:
                        gone.append(key)
                    elif present is True:
                        result.verified_present += 1
                        await _db(session_factory, lambda s, k=key: _mark_seen(s, org, spec.resource, [k], _now()))
                    else:
                        result.undecided += 1

            def remove(session: Session) -> None:
                if not gone:
                    return
                if spec.on_deleted is not None:
                    spec.on_deleted(session, gone)
                session.execute(
                    delete(SyncSeen).where(
                        and_(SyncSeen.org_id == org, SyncSeen.resource == spec.resource, SyncSeen.key.in_(gone))
                    )
                )

            await _db(session_factory, remove)
            result.deleted = gone
        await _db(
            session_factory,
            lambda s: s.execute(
                update(SyncCursor)
                .where(SyncCursor.org_id == org, SyncCursor.resource == spec.resource)
                .values(reconciled_at=started)
            ),
        )
        return result
    finally:
        await _db(session_factory, lambda s: _release(s, org, spec.resource, owner))


def cursor_status(session: Session, spec_or_resource: Any, *, org_id: Any = "") -> Optional[SyncCursor]:
    """The cursor row for a collection (for an admin card), or None if never walked."""
    resource = getattr(spec_or_resource, "resource", spec_or_resource)
    org = "" if org_id is None else str(org_id)
    return session.execute(
        select(SyncCursor).where(SyncCursor.org_id == org, SyncCursor.resource == resource)
    ).scalar_one_or_none()


async def refresh_keys(
    session_factory: Callable[[], Session],
    spec: SyncSpec,
    keys: Sequence[str],
    *,
    org_id: Any = "",
    now: Optional[datetime] = None,
) -> RefreshResult:
    """Read these records BY KEY and upsert them: no walk, no lease, the
    watermark untouched (the next pass still walks from it).

    For a change notification that names its record, where walking the
    collection to find one row would cost a walk. It can race a pass, which is
    safe ONLY when ``spec.upsert`` never replaces a newer row with an older one
    (see :func:`upsert_newer`); the keys found are marked seen, so a reconcile
    counts them as present. Needs the collection's optional ``fetch_keys``."""
    fetch_keys = getattr(spec.collection, "fetch_keys", None)
    if fetch_keys is None:
        raise TypeError(f"{spec.resource}: the collection has no fetch_keys(keys)")
    org = "" if org_id is None else str(org_id)
    wanted = list(dict.fromkeys(str(k) for k in keys if k not in (None, "")))
    result = RefreshResult(resource=spec.resource, asked=len(wanted))
    if not wanted:
        return result
    at = _utc(now) or _now()
    rows = [r for r in await fetch_keys(wanted) if _key_text(spec, r) is not None]
    found = [k for k in (_key_text(spec, r) for r in rows) if k is not None]

    def commit(session: Session) -> None:
        if rows:
            spec.upsert(session, rows)
            _mark_seen(session, org, spec.resource, found, at)

    await _db(session_factory, commit)
    result.found = list(dict.fromkeys(found))
    return result


def upsert_newer(
    session: Session,
    table: Any,
    rows: Sequence[dict[str, Any]],
    *,
    key_columns: Sequence[str],
    stamp_column: str,
) -> None:
    """``INSERT ... ON CONFLICT (key) DO UPDATE ... WHERE stamp <= new stamp``:
    an upsert that never replaces a newer row with an older version of itself.

    What makes :func:`refresh_keys` safe to race a walk: a page the walk read a
    moment ago must not overwrite the version a notification just read. EQUAL
    stamps still rewrite, so a full walk refills every column. ``table`` is a
    SQLAlchemy ``Table`` or a mapped model; Postgres and SQLite."""
    if not rows:
        return
    target = getattr(table, "__table__", table)
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
    else:
        from sqlalchemy.dialects.sqlite import insert as dialect_insert
    stmt = dialect_insert(target).values(list(rows))
    supplied = set().union(*(r.keys() for r in rows))
    updates = {
        c.name: stmt.excluded[c.name]
        for c in target.columns
        if c.name in supplied and c.name not in key_columns
    }
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=list(key_columns),
            set_=updates,
            where=target.c[stamp_column] <= stmt.excluded[stamp_column],
        )
    )


def saved_copy_is_current(
    cursor: Optional[SyncCursor],
    *,
    saved_stamp: Optional[datetime],
    mirrored_stamp: Optional[datetime],
) -> bool:
    """Whether a copy of a record saved elsewhere (its full body, say, kept
    beside the thin mirror) may be served instead of asking the remote.

    Only when the mirror can vouch for the record: the collection has been
    walked to the end at least once, its last pass completed without an error,
    and the copy was saved at a stamp at least the mirror's. A mirror that is
    behind (a failed or capped pass) vouches for nothing, and the caller asks
    the remote, as it would have without the copy."""
    if cursor is None or cursor.backfilled_at is None:
        return False
    if cursor.last_error is not None or not cursor.last_complete:
        return False
    saved, mirrored = _utc(saved_stamp), _utc(mirrored_stamp)
    if saved is None or mirrored is None:
        return False
    return saved >= mirrored
