"""0.19.0: ``importance`` is a catalogue, not an enum.

The four things worth pinning, in the order they would break:

1. **Equivalence.** A deployment that upgrades and adds no rung routes exactly
   as it did, because the two seeded rows carry the old built-in rule as data
   (``low`` no email, ``high`` email). This is the property that made the change
   safe to ship, so it is the first test.
2. **A rung an org invented actually routes**, both through the fallback (its
   own ``emails_by_default``) and as a matrix coordinate. Being able to SPELL a
   third rung is the whole point; being able to spell one that decides nothing
   would be the 0.18.0 complaint about ``normal`` all over again.
3. **An unseeded rung fails loud**, like an unseeded topic, and says where to
   fix it. Before 0.19.0 this was a ``ValueError`` from an enum constructor.
4. **The stored value survives a round trip.** The column used to be a
   SQLAlchemy ``Enum``, which raises on READ for a value outside its members, so
   "can it be written" and "can it be read back" were genuinely two questions.
"""

import pytest
from sqlmodel import select

import asas_notifications as notifications
from asas_notifications import service
from asas_notifications.models import (
    Notification,
    NotificationChannelPolicy,
    NotificationImportance,
)

from test_axes_routing import add_policy, deliveries, emit_axes


def add_rung(session, key, *, emails_by_default, org_id=None, rank=0):
    session.add(
        NotificationImportance(
            key=key,
            name=key.title(),
            emails_by_default=emails_by_default,
            org_id=org_id,
            rank=rank,
        )
    )
    session.commit()
    service.config_cache_clear()


def test_the_migration_seeds_the_two_rungs_that_were_the_enum(session):
    rows = {
        r.key: r
        for r in session.exec(
            select(NotificationImportance).where(NotificationImportance.org_id.is_(None))
        ).all()
    }
    assert set(rows) == {"low", "high"}
    # The old fallback, ``importance is not low``, as data.
    assert rows["low"].emails_by_default is False
    assert rows["high"].emails_by_default is True
    assert rows["low"].rank < rows["high"].rank


@pytest.mark.parametrize(
    "importance,expected",
    [("low", []), ("high", ["email"])],
)
def test_empty_policy_tables_route_exactly_as_before(session, importance, expected):
    row = emit_axes(session, ["u1"], "act.one", importance=importance)[0]
    assert deliveries(session, row.id) == expected


def test_an_org_rung_emails_off_its_own_row_with_no_policy_cell(session):
    """The fallback reads the catalogue, which is what makes a new rung mean
    something before an administrator has written a single cell."""
    add_rung(session, "critical", emails_by_default=True, rank=30)
    row = emit_axes(session, ["u1"], "act.two", importance="critical")[0]
    assert deliveries(session, row.id) == ["email"]


def test_a_quiet_org_rung_stays_in_the_feed(session):
    add_rung(session, "fyi", emails_by_default=False, rank=5)
    row = emit_axes(session, ["u1"], "act.three", importance="fyi")[0]
    assert deliveries(session, row.id) == []


def test_an_org_rung_is_a_real_matrix_coordinate(session):
    """A cell written against a rung the package never heard of resolves for it
    and for nothing else. Without this the catalogue would only be a relabelling
    exercise."""
    add_rung(session, "critical", emails_by_default=False, rank=30)
    add_policy(session, "email", topic="general", importance="critical", enabled=True)

    hit = emit_axes(session, ["u1"], "act.four", importance="critical")[0]
    assert deliveries(session, hit.id) == ["email"]

    # The cell names one rung, so the quiet seeded one is untouched by it.
    miss = emit_axes(session, ["u1"], "act.five", importance="low")[0]
    assert deliveries(session, miss.id) == []


def test_a_cell_on_one_rung_does_not_leak_onto_another(session):
    add_rung(session, "critical", emails_by_default=True, rank=30)
    add_policy(session, "email", importance="critical", enabled=False)

    muted = emit_axes(session, ["u1"], "act.six", importance="critical")[0]
    assert deliveries(session, muted.id) == []
    # ``high`` has no cell and keeps the fallback.
    loud = emit_axes(session, ["u1"], "act.seven", importance="high")[0]
    assert deliveries(session, loud.id) == ["email"]


def test_an_org_row_beats_the_platform_row_for_the_same_key(session):
    """The catalogue is DR 0001's shared-with-overrides pattern, like topics:
    one org may make ``high`` quiet without changing it for anybody else."""
    add_rung(session, "high", emails_by_default=False, org_id="org-1")
    quiet = emit_axes(session, ["u1"], "act.eight", importance="high", org_id="org-1")[0]
    assert deliveries(session, quiet.id) == []

    loud = emit_axes(session, ["u1"], "act.nine", importance="high", org_id="org-2")[0]
    assert deliveries(session, loud.id) == ["email"]


def test_an_unseeded_rung_fails_loud_and_names_the_table(session):
    with pytest.raises(LookupError) as excinfo:
        emit_axes(session, ["u1"], "act.ten", importance="urgent")
    assert "notification_importance" in str(excinfo.value)


def test_the_retired_middle_rung_gets_no_special_case(session):
    """0.18.0 retired ``normal`` and this does not quietly bring it back: an
    unconverted call site passing it must be told, not folded."""
    with pytest.raises(LookupError):
        emit_axes(session, ["u1"], "act.eleven", importance="normal")
    # ...unless a deployment deliberately seeds it, at which point it is an
    # ordinary rung and says for itself what it does.
    add_rung(session, "normal", emails_by_default=True, rank=15)
    row = emit_axes(session, ["u1"], "act.twelve", importance="normal")[0]
    assert deliveries(session, row.id) == ["email"]


def test_an_org_rung_reads_back_off_the_row(session):
    """The column was a SQLAlchemy ``Enum``, which raises on read for a value
    outside its members, so writing and reading were two separate questions."""
    add_rung(session, "critical", emails_by_default=True, rank=30)
    emitted = emit_axes(session, ["u1"], "act.thirteen", importance="critical")[0]
    session.expire_all()
    stored = session.exec(
        select(Notification).where(Notification.id == emitted.id)
    ).one()
    assert stored.importance == "critical"


def test_a_policy_cell_reads_back_off_the_row(session):
    add_rung(session, "critical", emails_by_default=False, rank=30)
    add_policy(session, "email", importance="critical", enabled=True)
    session.expire_all()
    cell = session.exec(
        select(NotificationChannelPolicy).where(
            NotificationChannelPolicy.importance == "critical"
        )
    ).one()
    assert cell.importance == "critical"


def test_a_rung_added_within_the_cache_ttl_is_found_by_a_fresh_requery(session):
    """The same guarantee ``topic`` has: a rung seeded on another replica inside
    the TTL window costs one extra SELECT, never a false LookupError that aborts
    the producer's transaction."""
    emit_axes(session, ["u1"], "act.fourteen", importance="high")  # warms the cache
    session.add(
        NotificationImportance(key="urgent", name="Urgent", emails_by_default=True)
    )
    session.commit()  # deliberately NO config_cache_clear()
    row = emit_axes(session, ["u1"], "act.fifteen", importance="urgent")[0]
    assert deliveries(session, row.id) == ["email"]


def test_a_rung_with_no_catalogue_row_at_all_is_treated_as_quiet(session):
    """A stored value whose row was deleted out from under it. The fallback
    under-delivers rather than over-delivers on purpose: an email cannot be
    recalled, and the notification is in the feed either way."""
    assert service._emails_by_default(session, None, "vanished") is False


def test_one_orgs_rung_override_never_decides_anothers(session):
    """The catalogue is cached whole and unscoped, so every org's override rows
    are in the same list. Falling back to "the first row that came back" would
    let one org's routing default decide another's."""
    add_rung(session, "high", emails_by_default=False, org_id="org-1")
    quiet = emit_axes(session, ["u1"], "act.sixteen", importance="high", org_id="org-1")[0]
    assert deliveries(session, quiet.id) == []
    # org-2 has no override, so it gets the PLATFORM row and not org-1's.
    loud = emit_axes(session, ["u1"], "act.seventeen", importance="high", org_id="org-2")[0]
    assert deliveries(session, loud.id) == ["email"]
