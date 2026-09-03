"""A host whose keys are NOT integers, which is the whole point of the change.

Every other test in this package passes integers. ``normalize_id`` turns those
into decimal strings, so they round-trip and every assertion holds whether the
conversion is applied or not — which means the int suite cannot tell a working
conversion from a missing one. Two real defects reached a consumer that way:

* an ownership check that compared the stored value against the host's own,
  giving ``"1" != 1``, so opening a notification answered 404 to its owner;
* a coalesce query that converted four of its five comparands, which SQLite
  accepted by column affinity and Postgres correctly refused.

Both are fixed. These tests exist so that reverting either fix FAILS, which
until now it did not. UUID hosts throughout, because a UUID is the case the
change was made for and the case no other test covers.
"""

import uuid

import pytest
from sqlmodel import select

import asas_notifications as notifications
from asas_notifications import channels, service
from asas_notifications.models import (
    Notification,
    NotificationChannelPolicy,
    NotificationTopic,
)


def uid() -> str:
    return str(uuid.uuid4())


@pytest.fixture()
def org() -> str:
    """A UUID tenant, installed as the ambient context for the test."""
    value = uid()
    service.configure_context_resolver(lambda s, _v=value: (0, _v))
    return value


def emit(session, action, recipients, **kw):
    kw.setdefault("topic", "general")
    kw.setdefault("nature", "info")
    kw.setdefault("urgency", "normal")
    kw.setdefault("title", "Hello")
    rows = notifications.notify(session, recipients, action, **kw)
    session.commit()
    return rows


# ── the ownership check (the 404-to-its-owner defect) ────────────────────────

def test_a_uuid_recipient_can_open_its_own_notification(session, org):
    """The defect exactly: ``_owned`` compares in Python, not SQL, so it is the
    one ownership test that does not inherit the conversion from the query."""
    user = uid()
    n = emit(session, "doc.shared", [user])[0]

    assert service.mark_read(session, user, n.id) is not None, (
        "a recipient was refused its own row — the ownership check is comparing "
        "the host's value against the stored one without normalising"
    )
    assert n.read_at is not None


def test_archive_and_unarchive_also_reach_a_uuid_recipients_row(session, org):
    """Every mutation routes through the same check, so the fix has to hold for
    all of them and not only the one that was reported."""
    user = uid()
    n = emit(session, "doc.shared", [user])[0]

    assert service.archive(session, user, n.id) is not None
    assert n.archived_at is not None
    assert service.unarchive(session, user, n.id) is not None
    assert n.archived_at is None


def test_a_stranger_is_still_refused(session, org):
    """The fix must not turn the check into a formality."""
    n = emit(session, "doc.shared", [uid()])[0]
    assert service.mark_read(session, uid(), n.id) is None


def test_a_cross_org_probe_is_indistinguishable_from_a_missing_row(session):
    """Both answer None, so the router answers 404 either way and never confirms
    that a row belongs to somebody else."""
    org_a, org_b, user = uid(), uid(), uid()
    service.configure_context_resolver(lambda s: (0, org_a))
    n = emit(session, "doc.shared", [user])[0]

    service.configure_context_resolver(lambda s: (0, org_b))
    assert service.mark_read(session, user, n.id) is None
    assert service.mark_read(session, user, n.id + 10_000) is None


# ── coalescing (the unconverted fifth comparand) ─────────────────────────────

def test_coalescing_folds_on_a_uuid_entity_id(session, org):
    """The defect: ``entity_id`` went into the query unconverted. On Postgres the
    comparison raises outright; on SQLite column affinity hid it. Either way the
    burst stopped folding."""
    user, entity = uid(), uid()

    first = emit(
        session, "doc.edited", [user],
        urgency="low", entity_type="doc", entity_id=entity,
        coalesce_unread=True, title="v1",
    )[0]
    again = emit(
        session, "doc.edited", [user],
        urgency="low", entity_type="doc", entity_id=entity,
        coalesce_unread=True, title="v2",
    )[0]

    assert again.id == first.id, "a second edit created a row instead of folding"
    assert again.title == "v2"
    assert len(session.exec(select(Notification)).all()) == 1


def test_coalescing_keeps_two_uuid_orgs_apart(session):
    """The org axis is part of the coalesce identity, so a shared entity id
    across tenants must not let one org's emit overwrite another's row."""
    org_a, org_b, user, entity = uid(), uid(), uid(), uid()

    service.configure_context_resolver(lambda s: (0, org_a))
    a = emit(
        session, "doc.edited", [user], urgency="low", entity_type="doc",
        entity_id=entity, coalesce_unread=True, title="org A",
    )[0]

    service.configure_context_resolver(lambda s: (0, org_b))
    b = emit(
        session, "doc.edited", [user], urgency="low", entity_type="doc",
        entity_id=entity, coalesce_unread=True, title="org B",
    )[0]

    assert b.id != a.id
    assert a.title == "org A", "org B's emit overwrote org A's row"


def test_a_uuid_entity_id_survives_the_round_trip(session, org):
    """Stored and read back unchanged: a UUID host gets its own key returned, not
    a reformatted one."""
    entity = uid()
    n = emit(
        session, "doc.shared", [uid()], entity_type="doc", entity_id=entity,
    )[0]
    assert n.entity_id == entity


# ── the feed, and the actor ──────────────────────────────────────────────────

def test_the_feed_and_the_unread_count_find_a_uuid_recipients_rows(session, org):
    """These go through the SQL conditions rather than the Python check, so they
    are the other half of the surface."""
    user, stranger = uid(), uid()
    emit(session, "doc.shared", [user])
    emit(session, "doc.shared", [stranger])

    rows, total = service.list_feed(session, user)
    assert total == 1
    assert [r.user_id for r in rows] == [user]
    assert service.unread_count(session, user) == 1


def test_a_uuid_actor_does_not_notify_itself(session, org):
    """Actor exclusion compares the actor against the recipients, both through
    the storage form. It is also the one identity argument that is never
    stored, which is why its annotation drifted back to ``int``."""
    actor, other = uid(), uid()
    rows = emit(session, "doc.shared", [actor, other], actor_user_id=actor)
    assert [r.user_id for r in rows] == [other]


# ── the configuration tables AK's #43 introduced ─────────────────────────────

def test_a_uuid_org_can_own_a_topic_and_a_policy_row(session, org):
    """The gap that made the change incomplete: the two new config tables kept
    integer org ids, so a UUID host could send notifications and still not
    write an org-scoped rule. That state is worse than not adopting at all —
    the product looks wired and the admin screen cannot save."""
    session.add(NotificationTopic(key="billing", name="Billing", org_id=org))
    session.add(
        NotificationChannelPolicy(channel="email", topic="billing", org_id=org, enabled=False)
    )
    session.commit()
    service.config_cache_clear()

    topics = session.exec(select(NotificationTopic).where(NotificationTopic.org_id == org)).all()
    assert [t.key for t in topics] == ["billing"]

    # and the rule is actually read back for this org, not merely stored
    n = emit(session, "invoice.due", [uid()], topic="billing")[0]
    assert "email" not in service.resolve_channels(
        session, org, topic="billing", urgency="normal",
    )
    assert n.org_id == org


def test_an_org_policy_row_does_not_leak_to_another_uuid_org(session):
    org_a, org_b = uid(), uid()
    session.add(NotificationTopic(key="billing", name="Billing", org_id=None))
    session.add(
        NotificationChannelPolicy(channel="email", topic="billing", org_id=org_a, enabled=False)
    )
    session.commit()

    service.configure_context_resolver(lambda s: (0, org_b))
    service.config_cache_clear()
    assert "email" in service.resolve_channels(
        session, org_b, topic="billing", urgency="normal",
    ), "org A's override suppressed email for org B"


# ── what reaches an adapter ──────────────────────────────────────────────────

def test_an_adapter_receives_the_hosts_own_identity_strings(session, org):
    """``DeliveryPayload`` is the package's outward contract, so its identity
    fields are the host's values in storage form — an adapter that wants an
    integer primary key coerces on its own side."""
    user = uid()
    seen: list[channels.DeliveryPayload] = []

    class Recorder:
        def send(self, payload):
            seen.append(payload)

    notifications.register_adapter("email", Recorder())
    emit(session, "doc.shared", [user])
    assert notifications.dispatch_pending(session.get_bind()) == 1

    assert seen, "nothing was dispatched"
    assert seen[0].recipient_user_id == user
    assert seen[0].org_id == org
    assert isinstance(seen[0].recipient_user_id, str)


# ── the contract for hosts that DO use integers ──────────────────────────────

def test_an_int_host_reads_back_decimal_strings(session):
    """Documented, not incidental: an int host keeps passing ints, and the values
    it reads back are their decimal strings. Anything relying on the old int
    type sees this at the boundary rather than in a subtle comparison."""
    service.configure_context_resolver(lambda s: (0, 7))
    n = emit(session, "doc.shared", [42], entity_type="doc", entity_id=99)[0]

    assert n.user_id == "42"
    assert n.org_id == "7"
    assert n.entity_id == "99"
    assert service.mark_read(session, 42, n.id) is not None
