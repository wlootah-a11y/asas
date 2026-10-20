"""Runtime behaviour: transitions, refusals and their structured fields, guards,
the sink, the catalog, the sentences and the process registry."""

import json
from enum import StrEnum

import pytest

import asas_lifecycle as lifecycle
from asas_lifecycle import (
    DefinitionError,
    Edge,
    GuardRefused,
    InvalidTransition,
    Lifecycle,
    UnknownStateError,
    guard,
)


class Doc(StrEnum):
    DRAFT = "draft"
    IN_REVIEW = "in_review"
    PUBLISHED = "published"
    ARCHIVED = "archived"


@guard("the reviewer is not the author")
def not_own_work(ctx) -> bool:
    return ctx["reviewer"] != ctx["author"]


@guard("the document has a title")
def has_title(ctx) -> bool:
    return bool(ctx.get("title"))


def make(sink=None, name="document", register=False):
    return Lifecycle(
        name,
        Doc,
        initial=Doc.DRAFT,
        terminal=Doc.ARCHIVED,
        sink=sink,
        register=register,
        description="An editorial document.",
        transitions=[
            (Doc.DRAFT, Doc.IN_REVIEW, "submit"),
            Edge(Doc.IN_REVIEW, Doc.PUBLISHED, "approve", guard=(has_title, not_own_work)),
            (Doc.IN_REVIEW, Doc.DRAFT, "return"),
            Edge((Doc.DRAFT, Doc.PUBLISHED), Doc.ARCHIVED, "archive"),
        ],
    )


@pytest.fixture(autouse=True)
def _isolated_registry():
    lifecycle.reset()
    yield
    lifecycle.reset()


OK = {"reviewer": "r", "author": "a", "title": "T"}


# ── transitions ─────────────────────────────────────────────────────────────


def test_transition_returns_the_record():
    rec = make().transition(Doc.DRAFT, Doc.IN_REVIEW)
    assert (rec.lifecycle, rec.from_state, rec.to_state, rec.action) == (
        "document", Doc.DRAFT, Doc.IN_REVIEW, "submit",
    )
    assert rec.as_dict() == {"lifecycle": "document", "from": "draft", "to": "in_review", "action": "submit"}


def test_strings_work_at_runtime():
    assert make().transition("draft", "in_review").to_state is Doc.IN_REVIEW


def test_fire_resolves_the_target():
    assert make().fire(Doc.IN_REVIEW, "return").to_state is Doc.DRAFT


def test_not_allowed_carries_structured_fields():
    with pytest.raises(InvalidTransition) as exc:
        make().transition(Doc.DRAFT, Doc.PUBLISHED)
    err = exc.value
    assert err.reason == "not_allowed"
    assert err.allowed == (Doc.IN_REVIEW, Doc.ARCHIVED)
    assert err.as_dict() == {
        "code": "invalid_transition",
        "message": "A document cannot move from draft to published",
        "lifecycle": "document",
        "from": "draft",
        "to": "published",
        "action": None,
        "allowed": ["in_review", "archived"],
        "reason": "not_allowed",
    }


def test_from_terminal():
    with pytest.raises(InvalidTransition) as exc:
        make().transition(Doc.ARCHIVED, Doc.DRAFT)
    assert exc.value.reason == "from_terminal"
    assert exc.value.allowed == ()


def test_fire_from_terminal():
    with pytest.raises(InvalidTransition) as exc:
        make().fire(Doc.ARCHIVED, "submit")
    assert exc.value.reason == "from_terminal"


def test_unknown_action_from_a_state():
    with pytest.raises(InvalidTransition) as exc:
        make().fire(Doc.DRAFT, "approve")
    assert (exc.value.reason, exc.value.action) == ("unknown_action", "approve")


def test_wrong_action_for_a_real_pair():
    with pytest.raises(InvalidTransition) as exc:
        make().transition(Doc.DRAFT, Doc.IN_REVIEW, action="archive")
    assert exc.value.reason == "unknown_action"
    assert "by submit" in exc.value.message


def test_unknown_state_is_not_a_no():
    lc = make()
    with pytest.raises(UnknownStateError) as exc:
        lc.can_transition("drafty", Doc.IN_REVIEW)
    assert exc.value.as_dict()["known"] == ["draft", "in_review", "published", "archived"]
    with pytest.raises(UnknownStateError):
        lc.allowed_from("nope")


# ── guards ──────────────────────────────────────────────────────────────────


def test_guards_pass():
    assert make().transition(Doc.IN_REVIEW, Doc.PUBLISHED, context=OK).action == "approve"


def test_guard_refusal_names_the_guard():
    with pytest.raises(GuardRefused) as exc:
        make().transition(Doc.IN_REVIEW, Doc.PUBLISHED, context={**OK, "reviewer": "a"})
    body = exc.value.as_dict()
    assert body["code"] == "transition_guard_refused"
    assert body["reason"] == "guard_refused"
    assert body["guard"] == "not_own_work"
    assert body["guard_description"] == "the reviewer is not the author"
    assert body["action"] == "approve"
    assert isinstance(exc.value, InvalidTransition)


def test_guards_run_in_order_and_stop_at_the_first_no():
    with pytest.raises(GuardRefused) as exc:
        make().transition(Doc.IN_REVIEW, Doc.PUBLISHED, context={"reviewer": "a", "author": "a"})
    assert exc.value.guard == "has_title"


def test_can_transition_applies_guards():
    lc = make()
    assert lc.can_transition(Doc.IN_REVIEW, Doc.PUBLISHED, context=OK)
    assert not lc.can_transition(Doc.IN_REVIEW, Doc.PUBLISHED, context={**OK, "title": ""})


def test_allowed_from_ignores_guards_available_applies_them():
    lc = make()
    assert lc.allowed_from(Doc.IN_REVIEW) == (Doc.PUBLISHED, Doc.DRAFT)
    assert [e.action for e in lc.available(Doc.IN_REVIEW, {**OK, "title": ""})] == ["return"]
    assert [e.action for e in lc.available(Doc.IN_REVIEW, OK)] == ["approve", "return"]


def test_unnamed_call_takes_the_first_passing_edge():
    class T(StrEnum):
        A = "a"
        B = "b"

    @guard("the move is urgent")
    def urgent(ctx):
        return ctx == "urgent"

    lc = Lifecycle("t", T, initial=T.A, terminal=T.B, register=False, transitions=[
        Edge(T.A, T.B, "expedite", guard=urgent),
        Edge(T.A, T.B, "normal"),
    ])
    assert lc.transition(T.A, T.B, context="urgent").action == "expedite"
    assert lc.transition(T.A, T.B, context=None).action == "normal"


# ── the sink ────────────────────────────────────────────────────────────────


def test_sink_gets_each_move_with_its_context():
    seen = []
    lc = make(sink=seen.append)
    lc.start(context="ctx0")
    lc.fire(Doc.DRAFT, "submit", context="ctx1")
    assert [(r.from_state, r.to_state, r.context) for r in seen] == [
        (None, Doc.DRAFT, "ctx0"),
        (Doc.DRAFT, Doc.IN_REVIEW, "ctx1"),
    ]


def test_no_sink_call_on_refusal_or_can_transition():
    seen = []
    lc = make(sink=seen.append)
    lc.can_transition(Doc.DRAFT, Doc.IN_REVIEW)
    with pytest.raises(InvalidTransition):
        lc.transition(Doc.DRAFT, Doc.PUBLISHED)
    with pytest.raises(GuardRefused):
        lc.transition(Doc.IN_REVIEW, Doc.PUBLISHED, context={**OK, "title": ""})
    assert seen == []


def test_a_failing_sink_propagates():
    def boom(record):
        raise RuntimeError("db down")

    with pytest.raises(RuntimeError, match="db down"):
        make(sink=boom).fire(Doc.DRAFT, "submit")


def test_start_defaults_to_the_only_initial_state():
    rec = make().start()
    assert (rec.from_state, rec.to_state, rec.action) == (None, Doc.DRAFT, None)
    with pytest.raises(InvalidTransition) as exc:
        make().start(Doc.PUBLISHED)
    assert exc.value.reason == "not_initial"
    assert exc.value.allowed == (Doc.DRAFT,)


def test_start_without_a_state_needs_a_single_initial():
    class T(StrEnum):
        A = "a"
        B = "b"
        C = "c"

    lc = Lifecycle("t", T, initial={T.A, T.B}, terminal=T.C, register=False,
                   transitions=[(T.A, T.C), (T.B, T.C)])
    with pytest.raises(InvalidTransition) as exc:
        lc.start()
    assert exc.value.reason == "not_initial"
    assert lc.start(T.B).to_state is T.B


# ── explanation ─────────────────────────────────────────────────────────────


def test_describe_renders_sentences():
    assert make().describe() == [
        "A document starts in draft.",
        "A document can move from draft to in review by submit.",
        "A document can move from in review to published by approve, only when "
        "the document has a title and the reviewer is not the author.",
        "A document can move from in review to draft by return.",
        "A document can move from draft to archived by archive.",
        "A document can move from published to archived by archive.",
        "Archived is final: a document never leaves it.",
    ]


def test_articles_and_self_edges():
    class T(StrEnum):
        OPEN = "open"
        SHUT = "shut"

    lc = Lifecycle("offer", T, initial=T.OPEN, terminal=T.SHUT, register=False,
                   transitions=[(T.OPEN, T.OPEN, "renew"), (T.OPEN, T.SHUT)])
    assert lc.describe()[:3] == [
        "An offer starts in open.",
        "An offer can re-enter open by renew.",
        "An offer can move from open to shut.",
    ]


def test_catalog_is_json_safe_and_complete():
    cat = make(sink=lambda r: None).catalog()
    json.dumps(cat)
    assert cat["name"] == "document"
    assert cat["initial"] == ["draft"] and cat["terminal"] == ["archived"]
    assert cat["actions"] == ["approve", "archive", "return", "submit"]
    assert cat["records_history"] is True
    review = next(s for s in cat["states"] if s["value"] == "in_review")
    assert review == {"value": "in_review", "initial": False, "terminal": False,
                      "next": ["published", "draft"], "actions": ["approve", "return"]}
    approve = next(t for t in cat["transitions"] if t["action"] == "approve")
    assert [g["name"] for g in approve["guards"]] == ["has_title", "not_own_work"]
    assert approve["sentence"].startswith("A document can move from in review to published")
    assert cat["sentences"] == make().describe()


def test_to_http_is_a_409_with_the_envelope():
    pytest.importorskip("fastapi")
    with pytest.raises(InvalidTransition) as exc:
        make().transition(Doc.DRAFT, Doc.PUBLISHED)
    http = lifecycle.to_http(exc.value)
    assert http.status_code == 409
    assert http.detail == exc.value.as_dict()


# ── the process registry ────────────────────────────────────────────────────


def test_construction_registers():
    lc = make(register=True)
    assert lifecycle.get("document") is lc
    assert list(lifecycle.lifecycles()) == ["document"]
    assert [c["name"] for c in lifecycle.catalog()] == ["document"]


def test_register_false_stays_private():
    make(register=False)
    assert lifecycle.lifecycles() == {}


def test_redefining_the_same_shape_is_fine():
    make(register=True)
    again = make(register=True, sink=lambda r: None)  # e.g. a module reload
    assert lifecycle.get("document") is again


def test_a_conflicting_name_fails_loud():
    make(register=True)
    with pytest.raises(DefinitionError) as exc:
        Lifecycle("document", Doc, initial=Doc.DRAFT, terminal=Doc.ARCHIVED,
                  transitions=[(Doc.DRAFT, Doc.IN_REVIEW), (Doc.IN_REVIEW, Doc.PUBLISHED),
                               (Doc.PUBLISHED, Doc.ARCHIVED)])
    assert exc.value.codes == ("duplicate_name",)


def test_get_unknown_names_the_known():
    make(register=True)
    with pytest.raises(KeyError, match="known: document"):
        lifecycle.get("nope")
