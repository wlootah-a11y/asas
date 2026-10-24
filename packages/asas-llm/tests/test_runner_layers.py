"""The runner with the gateway layers on: overrides and caps reach the
factory, the allowlist refuses before a client exists, the untrusted text is
marked for the guard, provenance is recorded, a busy gateway and a refusal are
raised as themselves, and a failure logs only what it is allowed to."""

import json
import logging

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from pydantic import BaseModel

from asas_llm import content_safety, provenance
from asas_llm.errors import GatewayBusy, GenerationFailed, ModelNotAllowed, UnsafeContent
from asas_llm.overrides import ModelAllowlist, ModelOverrides, OutputCaps
from asas_llm.prompts import InMemoryPromptStore, Prompt
from asas_llm.runners import LLMRunner, ModelOptions, static_model
from asas_llm.tracing import NullTracer
from conftest import run

MARKER = "MARKER-7f3a-name-text"


class Out(BaseModel):
    answer: str


class Factory:
    def __init__(self, responses):
        self.model = FakeListChatModel(responses=list(responses))
        self.asked: list[tuple[str, ModelOptions]] = []

    def __call__(self, model, options):
        self.asked.append((model, options))
        return self.model


def store():
    s = InMemoryPromptStore()
    s.chat("parse-cv", [("system", "Read {{brief}}."), ("user", "{{cv}}")])
    return s


def make(responses=('{"answer": "yes"}',), **kw):
    factory = Factory(responses)
    tracer = NullTracer()
    runner = LLMRunner(model_factory=factory, prompts=store(), tracer=tracer, default_model="gpt-5.1", **kw)
    return runner, factory, tracer


def test_overrides_and_caps_reach_the_factory_and_the_record():
    runner, factory, _ = make(overrides=ModelOverrides("parse-=gpt-4.1"), output_caps=OutputCaps("parse-=4096", default=100))
    with provenance.recording() as calls:
        result = run(runner.ainvoke("parse-cv", {"brief": "b", "cv": "c"}, output_model=Out, reasoning_effort="low", request_timeout=9.0, input_variable=None))
    (model, options) = factory.asked[0]
    assert model == "gpt-4.1" and options.max_tokens == 4096
    assert options.reasoning_effort == "low" and options.request_timeout == 9.0
    assert [type(c).__name__ for c in options.callbacks] == ["TruncatedOutputLogger"]
    assert result.model == "gpt-4.1" and result.provenance.model == "gpt-4.1"
    assert result.provenance.effort == "low" and result.provenance.prompt_source == "memory"
    assert [c.call for c in calls] == ["parse-cv"] and calls[0].trace_id == result.trace_id


def test_the_allowlist_refuses_before_a_client_exists():
    runner, factory, tracer = make(allowlist=ModelAllowlist("gpt-5.1"), overrides=ModelOverrides("parse-=gpt-4o"))
    with pytest.raises(ModelNotAllowed) as err:
        run(runner.ainvoke("parse-cv", {"brief": "b", "cv": "c"}, input_variable=None))
    assert err.value.model == "gpt-4o" and factory.asked == [] and tracer.traces == []


def test_the_untrusted_text_is_the_inputs_or_the_named_ones():
    seen = []

    def spy(_input, **_bound):
        seen.append(content_safety.UNTRUSTED.get())
        return AIMessage(content="ok")

    runner = LLMRunner(model_factory=static_model(RunnableLambda(spy)), prompts=store(), tracer=NullTracer(), default_model="m")
    run(runner.ainvoke("parse-cv", {"brief": "our brief", "cv": "Jane"}, input_variable=None))
    run(runner.ainvoke("parse-cv", {"brief": "our brief", "cv": "Jane"}, input_variable=None, untrusted_keys=("cv",)))
    assert seen == [["our brief", "Jane"], ["Jane"]]
    assert content_safety.UNTRUSTED.get() is None  # reset after the call


def _raising(error):
    def raise_(_input, **_bound):
        raise error

    return LLMRunner(model_factory=static_model(RunnableLambda(raise_)), prompts=store(), tracer=NullTracer(), default_model="m")


def test_a_busy_gateway_is_raised_as_such_and_a_refusal_as_itself():
    import httpx
    import openai

    request = httpx.Request("POST", "https://gateway.invalid/chat/completions")
    busy = _raising(openai.APIConnectionError(message="upstream", request=request))
    with pytest.raises(GatewayBusy) as err:
        run(busy.ainvoke("parse-cv", {"brief": "b", "cv": "c"}, input_variable=None))
    assert err.value.call == "parse-cv" and err.value.trace_id and isinstance(err.value.cause, openai.APIConnectionError)

    refused = _raising(RuntimeError("wrapped"))
    refusal = UnsafeContent("prompt", "parse-cv", ["jailbreak"])
    try:
        raise refusal
    except UnsafeContent as exc:
        wrapped = RuntimeError("graph node failed")
        wrapped.__cause__ = exc
    refused = _raising(wrapped)
    with pytest.raises(UnsafeContent) as err:
        run(refused.ainvoke("parse-cv", {"brief": "b", "cv": "c"}, input_variable=None))
    assert err.value is refusal


def test_a_hosts_own_refusal_class_is_recognised():
    class Mine(Exception):
        pass

    runner = _raising(Mine("no"))
    runner.refusal_kind = Mine
    with pytest.raises(Mine):
        run(runner.ainvoke("parse-cv", {"brief": "b", "cv": "c"}, input_variable=None))


def test_a_host_builds_its_own_busy_error():
    import httpx
    import openai

    class Busy(Exception):
        pass

    runner = _raising(openai.RateLimitError("slow down", response=httpx.Response(429, request=httpx.Request("POST", "http://x")), body=None))
    runner.busy_kind = lambda call, cause: Busy(call)
    with pytest.raises(Busy):
        run(runner.ainvoke("parse-cv", {"brief": "b", "cv": "c"}, input_variable=None))


def _log_text(caplog):
    parts = []
    for record in caplog.records:
        parts.append(record.getMessage())
        parts.append(repr(record.args))
        if record.exc_text:
            parts.append(record.exc_text)
    return "\n".join(parts) + "\n" + caplog.text


def test_without_detail_a_failure_logs_the_type_and_no_text(caplog):
    runner = _raising(RuntimeError(f"could not read {MARKER}"))
    with caplog.at_level(logging.DEBUG), pytest.raises(GenerationFailed) as err:
        run(runner.ainvoke("parse-cv", {"brief": "b", "cv": "c"}, input_variable=None, log_detail=False))
    assert MARKER not in _log_text(caplog) and MARKER not in str(err.value)
    assert "RuntimeError" in caplog.text and "parse-cv" in caplog.text
    assert all(r.exc_info is None for r in caplog.records)


def test_by_default_a_failure_logs_the_text(caplog):
    runner = _raising(RuntimeError(f"could not read {MARKER}"))
    with caplog.at_level(logging.DEBUG), pytest.raises(GenerationFailed):
        run(runner.ainvoke("parse-cv", {"brief": "b", "cv": "c"}, input_variable=None))
    assert MARKER in _log_text(caplog)


def test_an_answer_is_never_written_to_the_log(caplog):
    runner, _, _ = make([json.dumps({"answer": MARKER})])
    with caplog.at_level(logging.DEBUG):
        result = run(runner.ainvoke("parse-cv", {"brief": "b", "cv": "c"}, output_model=Out, input_variable=None))
    assert result.value.answer == MARKER
    assert MARKER not in _log_text(caplog)
    assert "llm_call" in caplog.text and "parse-cv" in caplog.text


def test_a_registry_prompt_records_its_version_and_a_local_one_does_not():
    served = Prompt(name="p", type="text", content="x", version=9, source="langfuse")
    runner, _, _ = make()
    result = run(runner.ainvoke(served, {}, input_variable=None))
    assert (result.provenance.prompt_version, result.provenance.prompt_source) == (9, "langfuse")
    local = Prompt(name="p", type="text", content="x", version=9, source="local")
    result = run(runner.ainvoke(local, {}, input_variable=None))
    assert result.provenance.prompt_version is None and result.provenance.prompt_sha
