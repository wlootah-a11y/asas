"""Per-call knobs read from one setting string, live, by longest prefix."""

import pytest

from asas_llm.errors import ModelNotAllowed
from asas_llm.overrides import ModelAllowlist, ModelOverrides, OutputCaps


def test_the_longest_prefix_wins_and_an_unnamed_call_keeps_its_model():
    overrides = ModelOverrides("screening-=gpt-4.1,screening-call-=gpt-5-mini")
    assert overrides.model_for("screening-call-summary", "gpt-5.1") == "gpt-5-mini"
    assert overrides.model_for("screening-questions", "gpt-5.1") == "gpt-4.1"
    assert overrides.model_for("parse-cv", "gpt-5.1") == "gpt-5.1"
    assert overrides.model_for(None, "gpt-5.1") == "gpt-5.1"


def test_a_live_setting_is_read_every_call():
    raw = {"value": ""}
    overrides = ModelOverrides(lambda: raw["value"])
    assert overrides.model_for("parse-cv", "gpt-5.1") == "gpt-5.1"
    raw["value"] = "parse-=gpt-4.1"
    assert overrides.model_for("parse-cv", "gpt-5.1") == "gpt-4.1"


def test_an_unknown_target_is_skipped_and_said_so(caplog):
    def known(model):
        if model not in {"gpt-4.1"}:
            raise ValueError(model)
        return model

    overrides = ModelOverrides("a=gpt-4.1,b=not-a-model", known=known)
    assert overrides.model_for("b-call", "gpt-5.1") == "gpt-5.1"
    assert overrides.model_for("a-call", "gpt-5.1") == "gpt-4.1"
    assert any("not-a-model" in r.getMessage() for r in caplog.records)


def test_a_malformed_pair_is_ignored():
    assert ModelOverrides("=x,,a=,b=gpt-4.1").model_for("b", "m") == "gpt-4.1"


def test_output_caps_fall_back_to_the_default_and_zero_is_no_cap():
    caps = OutputCaps("parse-=4096,bad=x", default=16384)
    assert caps.max_tokens_for("parse-cv") == 4096
    assert caps.max_tokens_for("score") == 16384
    assert OutputCaps("", default=0).max_tokens_for("anything") is None
    assert OutputCaps("a=0", default=100).max_tokens_for("a") is None


def test_the_allowlist_ignores_case_and_spaces_and_an_empty_list_approves_nothing():
    allow = ModelAllowlist(" GPT-5.1 , gpt-4.1")
    assert allow.allowed_models() == frozenset({"gpt-5.1", "gpt-4.1"})
    allow.ensure_allowed("gpt-5.1")
    with pytest.raises(ModelNotAllowed) as err:
        allow.ensure_allowed("gpt-4o", "chat")
    assert err.value.model == "gpt-4o" and err.value.purpose == "chat"
    with pytest.raises(ModelNotAllowed):
        ModelAllowlist("").ensure_allowed("gpt-5.1")


def test_the_allowlist_takes_an_iterable_a_callable_and_a_host_refusal():
    assert ModelAllowlist(["a", "B"]).is_allowed("b")
    live = {"models": "a"}
    allow = ModelAllowlist(lambda: live["models"], refusal=lambda m, p: KeyError((m, p)))
    allow.ensure_allowed("a")
    live["models"] = "c"
    with pytest.raises(KeyError):
        allow.ensure_allowed("a", "embedding")
