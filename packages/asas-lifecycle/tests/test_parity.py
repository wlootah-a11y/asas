"""Five hand-rolled status machines from a production system, expressed with
this package.

Each ``*_TABLE`` below is the shape such a system writes by hand: a
``_TRANSITIONS`` dict from a state to the states it may move to. Each
``*_LIFECYCLE`` is the same machine declared with ``asas_lifecycle``. The
tests assert that, for every ordered pair of states, the lifecycle allows
exactly what the table allowed and refuses exactly what it refused, and that
"terminal" means the same thing in both.

The five were chosen for the cases a table expresses badly, renamed into
neutral domains: an approval loop whose cancel is allowed only before going
live (a purchase request), two named moves into one state (a listing), a sink
that records every move (a claim), a self-edge, two entry points and a
"completed" that can be reopened (an appointment), and several exits shared by
two open states (a quote).
"""

from enum import StrEnum

import pytest

from asas_lifecycle import Edge, InvalidTransition, Lifecycle, guard

# ── purchase request: an approval loop, cancel only before it goes live ─────


class RequestStatus(StrEnum):
    NEW = "new"
    AWAITING_APPROVAL = "awaiting_approval"
    RETURNED = "returned"
    ACTIVE = "active"
    CLOSED = "closed"
    CANCELLED = "cancelled"


R = RequestStatus
REQUEST_TABLE = {
    R.NEW: frozenset({R.AWAITING_APPROVAL, R.ACTIVE, R.CANCELLED}),
    R.AWAITING_APPROVAL: frozenset({R.ACTIVE, R.RETURNED, R.CANCELLED}),
    R.RETURNED: frozenset({R.AWAITING_APPROVAL, R.ACTIVE, R.CANCELLED}),
    R.ACTIVE: frozenset({R.NEW, R.CLOSED}),
    R.CLOSED: frozenset(),
    R.CANCELLED: frozenset(),
}

REQUEST_LIFECYCLE = Lifecycle(
    "purchase_request",
    RequestStatus,
    initial=R.NEW,
    terminal={R.CLOSED, R.CANCELLED},
    transitions=[
        Edge(R.NEW, R.AWAITING_APPROVAL, "submit"),
        Edge(R.NEW, R.ACTIVE, "activate"),               # no approval needed
        Edge(R.AWAITING_APPROVAL, R.ACTIVE, "approve"),
        Edge(R.AWAITING_APPROVAL, R.RETURNED, "return"),
        Edge(R.RETURNED, R.AWAITING_APPROVAL, "submit"),  # resubmitted
        Edge(R.RETURNED, R.ACTIVE, "activate"),
        # Cancelled only from the three pre-live states. One edge.
        Edge((R.NEW, R.AWAITING_APPROVAL, R.RETURNED), R.CANCELLED, "cancel"),
        Edge(R.ACTIVE, R.NEW, "revert"),
        Edge(R.ACTIVE, R.CLOSED, "close"),
    ],
    register=False,
)

# ── listing: two named moves into one state ────────────────────────────────


class ListingStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    CLOSED = "closed"


L = ListingStatus
LISTING_TABLE = {
    L.DRAFT: frozenset({L.ACTIVE, L.CLOSED}),
    L.ACTIVE: frozenset({L.DRAFT, L.CLOSED}),
    L.CLOSED: frozenset(),
}

LISTING_LIFECYCLE = Lifecycle(
    "listing",
    ListingStatus,
    initial=L.DRAFT,
    terminal=L.CLOSED,
    transitions=[
        (L.DRAFT, L.ACTIVE, "publish"),
        (L.DRAFT, L.CLOSED, "discard"),
        (L.ACTIVE, L.DRAFT, "unpublish"),
        (L.ACTIVE, L.CLOSED, "discard"),
        (L.ACTIVE, L.CLOSED, "fill"),      # a second named edge, same pair
    ],
    register=False,
)

# ── claim: every move recorded by a sink ───────────────────────────────────


class ClaimStage(StrEnum):
    OPEN = "open"
    SETTLEMENT_OFFERED = "settlement_offered"
    CLOSED = "closed"


C = ClaimStage
CLAIM_TABLE = {
    C.OPEN: frozenset({C.SETTLEMENT_OFFERED, C.CLOSED}),
    C.SETTLEMENT_OFFERED: frozenset({C.OPEN, C.CLOSED}),
    C.CLOSED: frozenset(),
}

CLAIM_LIFECYCLE = Lifecycle(
    "claim",
    ClaimStage,
    initial=C.OPEN,
    terminal=C.CLOSED,
    transitions=[
        (C.OPEN, C.SETTLEMENT_OFFERED, "offer_settlement"),
        (C.SETTLEMENT_OFFERED, C.OPEN, "reopen"),
        Edge((C.OPEN, C.SETTLEMENT_OFFERED), C.CLOSED, "close"),
    ],
    register=False,
)

# ── appointment: a self-edge, two entry points, a reopenable "completed" ────


class AppointmentStatus(StrEnum):
    SCHEDULED = "scheduled"
    AWAITING_NOTES = "awaiting_notes"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    DRAFT = "draft"
    ABANDONED = "abandoned"


P = AppointmentStatus
APPOINTMENT_TABLE = {
    P.DRAFT: frozenset({P.SCHEDULED, P.ABANDONED}),
    P.SCHEDULED: frozenset({P.SCHEDULED, P.AWAITING_NOTES, P.COMPLETED, P.CANCELLED}),
    P.AWAITING_NOTES: frozenset({P.COMPLETED, P.CANCELLED}),
    P.COMPLETED: frozenset({P.SCHEDULED}),
    P.ABANDONED: frozenset(),
    P.CANCELLED: frozenset(),
}

APPOINTMENT_LIFECYCLE = Lifecycle(
    "appointment",
    AppointmentStatus,
    # A proposed slot creates a draft; booking directly creates the row
    # already scheduled. Two entry points, both declared.
    initial={P.DRAFT, P.SCHEDULED},
    terminal={P.ABANDONED, P.CANCELLED},
    transitions=[
        (P.DRAFT, P.SCHEDULED, "confirm"),
        (P.DRAFT, P.ABANDONED, "abandon"),
        (P.SCHEDULED, P.SCHEDULED, "reschedule"),        # a self-edge
        (P.SCHEDULED, P.AWAITING_NOTES, "save_notes_draft"),
        Edge((P.SCHEDULED, P.AWAITING_NOTES), P.COMPLETED, "submit_notes"),
        Edge((P.SCHEDULED, P.AWAITING_NOTES), P.CANCELLED, "cancel"),
        (P.COMPLETED, P.SCHEDULED, "reopen"),            # completed is NOT terminal
    ],
    register=False,
)

# ── quote: exits shared by two open states ─────────────────────────────────


class QuoteStatus(StrEnum):
    CREATED = "created"
    NEGOTIATING = "negotiating"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


Q = QuoteStatus
QUOTE_TABLE = {
    Q.CREATED: frozenset({Q.NEGOTIATING, Q.ACCEPTED, Q.REJECTED, Q.WITHDRAWN}),
    Q.NEGOTIATING: frozenset({Q.ACCEPTED, Q.REJECTED, Q.WITHDRAWN}),
    Q.ACCEPTED: frozenset(),
    Q.REJECTED: frozenset(),
    Q.WITHDRAWN: frozenset(),
}

OPEN = (Q.CREATED, Q.NEGOTIATING)
QUOTE_LIFECYCLE = Lifecycle(
    "quote",
    QuoteStatus,
    initial=Q.CREATED,
    terminal={Q.ACCEPTED, Q.REJECTED, Q.WITHDRAWN},
    transitions=[
        (Q.CREATED, Q.NEGOTIATING, "negotiate"),
        Edge(OPEN, Q.ACCEPTED, "accept"),
        Edge(OPEN, Q.REJECTED, "reject"),
        Edge(OPEN, Q.WITHDRAWN, "withdraw"),
    ],
    register=False,
)


MACHINES = {
    "purchase_request": (REQUEST_TABLE, REQUEST_LIFECYCLE),
    "listing": (LISTING_TABLE, LISTING_LIFECYCLE),
    "claim": (CLAIM_TABLE, CLAIM_LIFECYCLE),
    "appointment": (APPOINTMENT_TABLE, APPOINTMENT_LIFECYCLE),
    "quote": (QUOTE_TABLE, QUOTE_LIFECYCLE),
}


def _pairs(table):
    states = list(table)
    return [(a, b) for a in states for b in states]


@pytest.mark.parametrize("name", MACHINES)
def test_allows_exactly_what_the_table_allows(name):
    table, lifecycle = MACHINES[name]
    for a, b in _pairs(table):
        assert lifecycle.can_transition(a, b) is (b in table[a]), (name, a, b)


@pytest.mark.parametrize("name", MACHINES)
def test_every_refused_pair_raises_with_the_allowed_set(name):
    table, lifecycle = MACHINES[name]
    refused = [(a, b) for a, b in _pairs(table) if b not in table[a]]
    assert refused  # every machine refuses something
    for a, b in refused:
        with pytest.raises(InvalidTransition) as exc:
            lifecycle.transition(a, b)
        err = exc.value
        assert (err.lifecycle, err.from_state, err.to_state) == (name, a, b)
        assert set(err.allowed) == set(table[a])


@pytest.mark.parametrize("name", MACHINES)
def test_every_allowed_pair_transitions(name):
    table, lifecycle = MACHINES[name]
    for a, b in _pairs(table):
        if b in table[a]:
            record = lifecycle.transition(a, b)
            assert (record.from_state, record.to_state) == (a, b)


@pytest.mark.parametrize("name", MACHINES)
def test_allowed_from_matches_the_table(name):
    table, lifecycle = MACHINES[name]
    for state, targets in table.items():
        assert set(lifecycle.allowed_from(state)) == targets, (name, state)


@pytest.mark.parametrize("name", MACHINES)
def test_terminal_means_no_way_out_in_both(name):
    """A hand-rolled ``is_terminal`` is ``not _TRANSITIONS[s]``."""
    table, lifecycle = MACHINES[name]
    for state, targets in table.items():
        assert lifecycle.is_terminal(state) is (not targets), (name, state)


def test_appointment_keeps_its_self_edge_and_reopenable_completed():
    lc = APPOINTMENT_LIFECYCLE
    assert lc.fire(P.SCHEDULED, "reschedule").to_state is P.SCHEDULED
    assert not lc.is_terminal(P.COMPLETED)
    assert lc.fire(P.COMPLETED, "reopen").to_state is P.SCHEDULED
    # both entry points start a record; a mid-life state does not
    assert lc.start(P.DRAFT).from_state is None
    assert lc.start(P.SCHEDULED).to_state is P.SCHEDULED
    with pytest.raises(InvalidTransition) as exc:
        lc.start(P.COMPLETED)
    assert exc.value.reason == "not_initial"


def test_listing_close_keeps_both_causes_as_actions():
    """Discarded and filled are one pair, two named moves; the record says
    which one happened."""
    assert LISTING_LIFECYCLE.fire(L.ACTIVE, "fill").action == "fill"
    assert LISTING_LIFECYCLE.transition(L.ACTIVE, L.CLOSED, action="discard").action == "discard"


def test_request_cancel_is_pre_live_only():
    """Cancelled from the three pre-live states, closed only from active."""
    lc = REQUEST_LIFECYCLE
    for s in (R.NEW, R.AWAITING_APPROVAL, R.RETURNED):
        assert lc.fire(s, "cancel").to_state is R.CANCELLED
    with pytest.raises(InvalidTransition) as exc:
        lc.fire(R.ACTIVE, "cancel")
    assert exc.value.reason == "unknown_action"
    assert set(exc.value.allowed) == {R.NEW, R.CLOSED}


def test_a_service_rule_becomes_a_guard():
    """Unpublish only while the listing has never had an order. A hand-rolled
    machine enforces that in the service beside the table; here it sits on the
    edge and is refused with its own sentence."""

    @guard("the listing has never received an order")
    def never_ordered(ctx) -> bool:
        return ctx["orders"] == 0

    lc = Lifecycle(
        "listing", ListingStatus, initial=L.DRAFT, terminal=L.CLOSED, register=False,
        transitions=[
            (L.DRAFT, L.ACTIVE, "publish"),
            (L.DRAFT, L.CLOSED, "discard"),
            Edge(L.ACTIVE, L.DRAFT, "unpublish", guard=never_ordered),
            (L.ACTIVE, L.CLOSED, "discard"),
        ],
    )
    assert lc.can_transition(L.ACTIVE, L.DRAFT, context={"orders": 0})
    assert not lc.can_transition(L.ACTIVE, L.DRAFT, context={"orders": 3})
    assert "only when the listing has never received an order" in " ".join(lc.describe())


def test_claim_stage_events_come_from_the_sink():
    """One stage-event row per move, with a NULL ``from_stage`` for the
    creation event: the history table a hand-rolled machine writes itself,
    here host-persisted from the sink."""
    rows = []

    def record(event):
        ctx = event.context
        rows.append({
            "claim_id": ctx["claim_id"],
            "from_stage": event.from_state,
            "to_stage": event.to_state,
            "actor": ctx["actor"],
            "detail": {"action": event.action},
        })

    lc = Lifecycle(
        "claim", ClaimStage, initial=C.OPEN, terminal=C.CLOSED,
        register=False, sink=record,
        transitions=[
            (C.OPEN, C.SETTLEMENT_OFFERED, "offer_settlement"),
            (C.SETTLEMENT_OFFERED, C.OPEN, "reopen"),
            Edge((C.OPEN, C.SETTLEMENT_OFFERED), C.CLOSED, "close"),
        ],
    )
    ctx = {"claim_id": 7, "actor": "agent@example.org"}
    lc.start(context=ctx)
    lc.fire(C.OPEN, "offer_settlement", context=ctx)
    lc.fire(C.SETTLEMENT_OFFERED, "reopen", context=ctx)
    with pytest.raises(InvalidTransition):
        lc.transition(C.CLOSED, C.OPEN, context=ctx)   # refused: nothing written
    assert [(r["from_stage"], r["to_stage"]) for r in rows] == [
        (None, C.OPEN),
        (C.OPEN, C.SETTLEMENT_OFFERED),
        (C.SETTLEMENT_OFFERED, C.OPEN),
    ]
