"""Fixtures for the reference host's suite.

Engine selection mirrors the package suites (SQLite by default, Postgres when
``TEST_DATABASE_URL`` is set), because "the host contract holds" is a claim
about both engines or it is not worth making.

The app is built per-test against a fresh database. That is slower than a
session-scoped client and it is the right trade here: the boot *sequence* is
half of what this suite verifies, so it has to actually run each time.
"""

from __future__ import annotations

import importlib
import os
import tempfile
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlmodel import Session, create_engine

_TEST_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture()
def database_url(tmp_path):
    if _TEST_URL:
        # This drops every table in the target database, once per test. Refuse
        # unless the database name says it is disposable: pointing
        # TEST_DATABASE_URL at a shared development database is an easy mistake
        # to make and there is no recovery path from it.
        name = make_url(_TEST_URL).database or ""
        if "test" not in name:
            raise RuntimeError(
                f"refusing to reset database {name!r}: TEST_DATABASE_URL must name a "
                f"database with 'test' in it, because this fixture drops its schema."
            )
        eng = create_engine(_TEST_URL)
        with eng.begin() as conn:
            # IF EXISTS: a freshly created database may have no public schema,
            # and the bare form raises there.
            conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
        eng.dispose()
        return _TEST_URL
    return f"sqlite:///{tmp_path / 'helpdesk.db'}"


def _reload_app(monkeypatch, database_url, tmp_path, **env):
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path / "uploads"))
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    import app.config
    import app.db
    import app.fake_auth
    import app.main

    importlib.reload(app.config)
    importlib.reload(app.db)
    # fake_auth holds `settings` and `get_session` by reference, so it must be
    # in the reload set: a stale `get_session` is a *different* dependency
    # callable, FastAPI caches per callable, and the actor stashed on one
    # request session is invisible to the other — auth silently half-works.
    importlib.reload(app.fake_auth)
    return importlib.reload(app.main)


@pytest.fixture()
def app_module(database_url, tmp_path, monkeypatch):
    """Re-import the app against this test's database.

    The reload is load-bearing. ``app.config.settings`` and ``app.db.engine`` are
    module-level singletons — as they are in most real hosts — so pointing the
    app at a different database means re-importing, not mutating. Reloading also
    re-runs the registrations, which is what makes the boot-order tests honest.
    """
    return _reload_app(monkeypatch, database_url, tmp_path)


@pytest.fixture()
def fake_auth_app(database_url, tmp_path, monkeypatch):
    """The same app with fake auth armed, for tests about *who* is calling.

    Booting it seeds the demo agents, so tokens like ``token-agent`` resolve.
    A separate fixture rather than a flag on ``app_module``: most of the suite
    is about the anonymous default posture, and should stay in it.
    """
    return _reload_app(monkeypatch, database_url, tmp_path, ENABLE_FAKE_AUTH="1")


@pytest.fixture()
def client(app_module):
    from fastapi.testclient import TestClient

    with TestClient(app_module.app) as c:
        yield c


@pytest.fixture()
def session(app_module):
    with Session(app_module.engine) as s:
        yield s


@pytest.fixture()
def agents(client, app_module):
    """Three agents spanning the roles the policy distinguishes.

    Created through the session rather than an endpoint because agent
    provisioning is not what any of these tests are about.
    """
    from app.models import Agent

    rows = [
        Agent(name="Ada", email="admin@example.invalid", role="admin"),
        Agent(name="Lin", email="lead@example.invalid", role="member"),
        Agent(name="Sam", email="agent@example.invalid", role="member"),
        Agent(name="Vic", email="viewer@example.invalid", role="viewer"),
    ]
    with Session(app_module.engine) as s:
        for row in rows:
            s.add(row)
        s.commit()
        for row in rows:
            s.refresh(row)
        return {r.email.split("@")[0]: r for r in rows}
