"""Fixtures: SQLite temp file by default, Postgres when TEST_DATABASE_URL is set;
the schema always comes from the package's own chain. A fake remote stands in
for the API and can misbehave the ways real ones do."""

import os
import random
import tempfile
import uuid
from datetime import datetime, timedelta
from typing import Any, Optional

import pytest
from sqlalchemy import text
from sqlmodel import Session, create_engine

import asas_sync
from asas_sync import RemotePage, SyncSpec

TEST_URL = os.environ.get("TEST_DATABASE_URL")
T0 = datetime(2026, 9, 1, 12, 0, 0)


@pytest.fixture()
def engine():
    if TEST_URL:
        eng = create_engine(TEST_URL)
        with eng.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
    else:
        path = os.path.join(tempfile.gettempdir(), f"asas_sync_{uuid.uuid4().hex}.db")
        eng = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    yield eng
    eng.dispose()
    if not TEST_URL:
        os.unlink(path)


@pytest.fixture()
def factory(engine):
    asas_sync.migrate(engine)
    return lambda: Session(engine)


class FakeRemote:
    """``rows``: key -> stamp. Options mimic real APIs: ``shuffle_ties`` (tie
    order differs per request), ``ceiling`` (stops serving at an offset and
    says has_more=False there), ``ignore_after_key`` (a broken key filter)."""

    def __init__(self, n: int = 0, *, key_ordered: bool, step_s: float = 60, shuffle_ties: bool = False,
                 ceiling: Optional[int] = None, ignore_after_key: bool = False, seed: int = 7):
        self.rows: dict[int, datetime] = {k: T0 - timedelta(days=1) + timedelta(seconds=k * step_s) for k in range(1, n + 1)}
        self.key_ordered = key_ordered
        self.shuffle_ties = shuffle_ties
        self.ceiling = ceiling
        self.ignore_after_key = ignore_after_key
        self.random = random.Random(seed)
        self.requests: list[dict[str, Any]] = []
        self.exists_answer: dict[str, Optional[bool]] = {}
        self.during_fetch = None

    async def fetch(self, *, since, after_key, offset, limit, purpose):
        self.requests.append(dict(since=since, after_key=after_key, offset=offset, limit=limit, purpose=purpose))
        if self.during_fetch is not None:
            self.during_fetch(self, len(self.requests))
        items = [(k, s) for k, s in self.rows.items() if since is None or s >= since]
        if self.key_ordered:
            if after_key is not None and not self.ignore_after_key:
                items = [(k, s) for k, s in items if k > int(after_key)]
            items.sort()
        else:
            items.sort(key=lambda it: (it[1], self.random.random() if self.shuffle_ties else it[0]))
        if self.ceiling is not None:
            items = items[: self.ceiling]
        page = items[offset : offset + limit]
        has_more = offset + limit < len(items)
        return RemotePage(rows=[{"id": k, "updated": s} for k, s in page], has_more=has_more)

    async def exists(self, key):
        if key in self.exists_answer:
            return self.exists_answer[key]
        return int(key) in self.rows


@pytest.fixture()
def mirror():
    return {}


def make_spec(remote: FakeRemote, mirror: dict, **over) -> SyncSpec:
    def upsert(session, rows):
        for r in rows:
            mirror[str(r["id"])] = r["updated"]

    def on_deleted(session, keys):
        for k in keys:
            mirror.pop(k, None)

    base = dict(resource="people", collection=remote, key=lambda r: r["id"], stamp=lambda r: r["updated"],
                upsert=upsert, on_deleted=on_deleted, key_ordered=remote.key_ordered, page_size=10, clock_skew_s=0)
    base.update(over)
    return SyncSpec(**base)
