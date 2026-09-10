"""Appending, reading back, and the transaction promise."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlmodel import Session

import asas_audit
import asas_tenancy
from conftest import ORG_A, ORG_B, append_and_commit


def test_append_records_the_facts(session):
    row = asas_audit.append(
        session, org_id=ORG_A, actor="alice", action="invoice.approved",
        resource_type="invoice", resource_id="42", payload={"amount": 100},
    )
    session.commit()

    assert row.seq is not None, "the database assigns the chain order"
    assert row.id and len(row.id) == 36, "and a UUID identity of its own"
    assert row.action == "invoice.approved"
    assert row.payload == {"amount": 100}
    assert row.hash_prev is None, "the first entry in a chain links to nothing"
    assert len(bytes(row.hash_current)) == 32


def test_the_entry_commits_with_the_change(migrated):
    """The central promise: one transaction decides the fate of both, so there is
    no state in which the change happened and the record did not."""
    with Session(migrated) as s:
        asas_tenancy.set_tenant_guc(s, ORG_A)
        asas_audit.append(
            s, org_id=ORG_A, actor="alice", action="thing.done",
            resource_type="thing", resource_id="1",
        )
        s.rollback()

    with Session(migrated) as s:
        asas_tenancy.set_tenant_guc(s, ORG_A)
        assert asas_audit.history(s, org_id=ORG_A) == []


def test_append_does_not_commit_on_its_own(session):
    """Stated as a test because a helpful ``commit()`` inside ``append`` would
    silently break the promise above, and nothing else would notice."""
    asas_audit.append(
        session, org_id=ORG_A, actor="alice", action="thing.done",
        resource_type="thing", resource_id="1",
    )
    assert session.in_transaction()
    session.rollback()


def test_entries_chain_in_order(migrated):
    for i in range(4):
        append_and_commit(migrated, ORG_A, action=f"step.{i}")
    with Session(migrated) as s:
        asas_tenancy.set_tenant_guc(s, ORG_A)
        rows = list(reversed(asas_audit.history(s, org_id=ORG_A)))
        assert [r.hash_prev for r in rows][0] is None
        for prev, nxt in zip(rows, rows[1:]):
            assert bytes(nxt.hash_prev) == bytes(prev.hash_current)


def test_history_is_newest_first(migrated):
    for i in range(3):
        append_and_commit(migrated, ORG_A, action=f"step.{i}")
    with Session(migrated) as s:
        asas_tenancy.set_tenant_guc(s, ORG_A)
        actions = [r.action for r in asas_audit.history(s, org_id=ORG_A)]
    assert actions == ["step.2", "step.1", "step.0"]


def test_history_filters(migrated):
    with Session(migrated) as s:
        asas_tenancy.set_tenant_guc(s, ORG_A)
        asas_audit.append(s, org_id=ORG_A, actor="alice", action="a",
                          resource_type="invoice", resource_id="1")
        asas_audit.append(s, org_id=ORG_A, actor="bob", action="b",
                          resource_type="invoice", resource_id="2")
        asas_audit.append(s, org_id=ORG_A, actor="alice", action="c",
                          resource_type="order", resource_id="1")
        s.commit()

        assert len(asas_audit.history(s, org_id=ORG_A, actor="alice")) == 2
        assert len(asas_audit.history(s, org_id=ORG_A, action="b")) == 1
        assert len(asas_audit.history(s, org_id=ORG_A, resource_type="invoice")) == 2
        assert len(asas_audit.history(
            s, org_id=ORG_A, resource_type="invoice", resource_id="1")) == 1


def test_a_resource_id_without_its_type_is_refused(session):
    """An id is unique only within a type, so half the address of a record is a
    query that quietly matches another type's row."""
    with pytest.raises(ValueError, match="needs resource_type"):
        asas_audit.history(session, org_id=ORG_A, resource_id="1")


def test_occurred_at_can_predate_the_write(session):
    """A queued job records something that happened earlier. It is part of the
    hash, so it cannot be corrected afterwards, which is the point."""
    when = datetime(2026, 1, 1, tzinfo=timezone.utc)
    row = asas_audit.append(
        session, org_id=ORG_A, actor="worker", action="thing.done",
        resource_type="thing", resource_id="1", occurred_at=when,
    )
    session.commit()
    assert row.occurred_at == when
    assert asas_audit.verify(session, ORG_A).is_intact


def test_each_tenant_has_its_own_chain(migrated):
    """Chains are per tenant, so one tenant's first entry links to nothing even
    when another tenant already has a history."""
    append_and_commit(migrated, ORG_A)
    with Session(migrated) as s:
        asas_tenancy.set_tenant_guc(s, ORG_B)
        row = asas_audit.append(
            s, org_id=ORG_B, actor="carol", action="thing.done",
            resource_type="thing", resource_id="1",
        )
        s.commit()
        assert row.hash_prev is None
        assert asas_audit.verify(s, ORG_B).is_intact


def test_verify_over_the_stored_rows(migrated):
    for i in range(5):
        append_and_commit(migrated, ORG_A, action=f"step.{i}")
    with Session(migrated) as s:
        asas_tenancy.set_tenant_guc(s, ORG_A)
        report = asas_audit.verify(s, ORG_A)
    assert report.events_checked == 5
    assert report.is_intact
