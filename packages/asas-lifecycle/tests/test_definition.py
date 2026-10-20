"""Definition-time validation: a wrong lifecycle fails at construction, with
every problem listed and a stable code for each."""

from enum import Enum, StrEnum

import pytest

from asas_lifecycle import DefinitionError, Edge, Guard, Lifecycle


class S(StrEnum):
    A = "a"
    B = "b"
    C = "c"


class Other(StrEnum):
    A = "a"


def build(**kw):
    kw.setdefault("initial", S.A)
    kw.setdefault("terminal", S.C)
    kw.setdefault("transitions", [(S.A, S.B), (S.B, S.C)])
    return Lifecycle("thing", S, register=False, **kw)


def codes(**kw):
    with pytest.raises(DefinitionError) as exc:
        build(**kw)
    return exc.value.codes


def test_a_valid_definition_builds():
    lc = build()
    assert lc.allowed_from(S.A) == (S.B,)


def test_string_values_are_accepted_for_states():
    lc = build(initial="a", terminal=["c"], transitions=[("a", "b"), ("b", "c")])
    assert lc.allowed_from("a") == (S.B,)


def test_unknown_state_in_a_transition():
    assert "unknown_state" in codes(transitions=[(S.A, S.B), (S.B, S.C), (S.A, "z")])


def test_a_member_of_another_enum_is_unknown():
    assert "unknown_state" in codes(transitions=[(S.A, S.B), (S.B, S.C), (Other.A, S.B)])


def test_unknown_initial_and_terminal():
    assert codes(initial="z", terminal="y").count("unknown_state") == 2


def test_unreachable_state():
    assert codes(transitions=[(S.A, S.C)]) == ("dead_end", "unreachable_state")


def test_unreachable_only():
    assert codes(transitions=[(S.A, S.C), (S.B, S.C)]) == ("unreachable_state",)


def test_terminal_with_outgoing_edges():
    assert "terminal_has_exits" in codes(transitions=[(S.A, S.B), (S.B, S.C), (S.C, S.A)])


def test_an_undeclared_sink_is_a_dead_end():
    assert codes(terminal=()) == ("dead_end",)


def test_no_initial_state():
    assert "no_initial" in codes(initial=())


def test_duplicate_edge():
    assert "duplicate_edge" in codes(transitions=[(S.A, S.B), (S.A, S.B), (S.B, S.C)])


def test_same_pair_with_two_actions_is_not_a_duplicate():
    lc = build(transitions=[(S.A, S.B, "x"), (S.A, S.B, "y"), (S.B, S.C)])
    assert [e.action for e in lc.edges_from(S.A)] == ["x", "y"]


def test_one_action_leading_two_places_is_ambiguous():
    assert "ambiguous_action" in codes(
        transitions=[(S.A, S.B, "go"), (S.A, S.C, "go"), (S.B, S.C)]
    )


def test_empty_action_is_refused():
    assert "invalid_action" in codes(transitions=[(S.A, S.B, ""), (S.B, S.C)])


def test_malformed_edge():
    assert "invalid_edge" in codes(transitions=[(S.A,), (S.A, S.B), (S.B, S.C)])


def test_a_lambda_guard_has_no_sentence():
    assert "guard_undescribed" in codes(
        transitions=[Edge(S.A, S.B, guard=lambda ctx: True), (S.B, S.C)]
    )


def test_a_guard_with_an_empty_description():
    g = Guard("g", lambda ctx: True, " ")
    assert "guard_undescribed" in codes(transitions=[Edge(S.A, S.B, guard=g), (S.B, S.C)])


def test_a_docstring_function_is_a_guard():
    def is_ready(ctx):
        """the thing is ready."""
        return True

    lc = build(transitions=[Edge(S.A, S.B, guard=is_ready), (S.B, S.C)])
    (g,) = lc.edges_from(S.A)[0].guards
    assert (g.name, g.description) == ("is_ready", "the thing is ready")


def test_not_a_guard():
    assert "invalid_guard" in codes(transitions=[Edge(S.A, S.B, guard=3), (S.B, S.C)])


def test_states_must_be_an_enum():
    with pytest.raises(DefinitionError) as exc:
        Lifecycle("x", dict, initial="a", transitions=[], register=False)
    assert exc.value.codes == ("states_not_enum",)


def test_state_values_must_be_strings():
    class N(Enum):
        A = 1
        B = 2

    with pytest.raises(DefinitionError) as exc:
        Lifecycle("x", N, initial=N.A, terminal=N.B, transitions=[(N.A, N.B)], register=False)
    assert "state_value_not_str" in exc.value.codes


def test_empty_name():
    assert "invalid_name" in pytest.raises(
        DefinitionError, Lifecycle, "", S, initial=S.A, terminal=S.C,
        transitions=[(S.A, S.B), (S.B, S.C)], register=False,
    ).value.codes


def test_every_problem_is_reported_at_once():
    with pytest.raises(DefinitionError) as exc:
        build(transitions=[(S.A, "z"), (S.C, S.A)])
    err = exc.value
    assert set(err.codes) >= {"unknown_state", "terminal_has_exits", "dead_end", "unreachable_state"}
    body = err.as_dict()
    assert body["code"] == "lifecycle_definition_invalid"
    assert body["lifecycle"] == "thing"
    assert {p["code"] for p in body["problems"]} == set(err.codes)
    assert isinstance(err, ValueError)
