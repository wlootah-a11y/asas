"""0.6.0: the SDK's run traces reach Langfuse over OpenTelemetry, the same mapping
as the 2.x processor. A real Langfuse client (SDK v3+) exports into
OpenTelemetry's in-memory exporter, so the tests read the span attributes that
would go on the wire: the trace id, the nesting, the observation types, the
trace's user, session, tags, version and metadata on every observation, the
prompt link, the mask, and nothing on a span of the host's."""

import json
import uuid

import pytest

langfuse = pytest.importorskip("langfuse")
if not hasattr(langfuse, "propagate_attributes"):
    pytest.skip("needs the Langfuse SDK v3 or later", allow_module_level=True)

from agents import set_trace_processors  # noqa: E402
from agents.tracing import (  # noqa: E402
    agent_span,
    custom_span,
    function_span,
    generation_span,
    trace,
)
from conftest import run  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)
from test_loop import RENDERER, never_asked  # noqa: E402
from test_tracing_attributes import (  # noqa: E402
    SOURCE,
    SpanOpeningModel,
    no_tools,
    snapshot,
)

from asas_agent import LangfuseOtelTracingProcessor, run_agent  # noqa: E402
from asas_agent.tracing import MASK_FAILED, langfuse_id  # noqa: E402

TRACE_ID = "trace_" + "c" * 32


class Wire:
    """A Langfuse client whose spans land in memory, and what landed."""

    def __init__(self):
        self.exporter = InMemorySpanExporter()
        self.client = langfuse.Langfuse(
            public_key=f"pk-{uuid.uuid4().hex}",
            secret_key="sk-test",
            base_url="http://localhost:9",
            tracer_provider=TracerProvider(),
            span_exporter=self.exporter,
        )

    def spans(self):
        self.client.flush()
        return list(self.exporter.get_finished_spans())

    def named(self, name):
        [span] = [s for s in self.spans() if s.name == name]
        return span

    def by_type(self, kind):
        return [
            s
            for s in self.spans()
            if s.attributes.get("langfuse.observation.type") == kind
        ]


def attribute(span, key):
    """A span attribute, JSON-decoded when the SDK encoded it."""
    value = span.attributes.get(key)
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


@pytest.fixture
def wire():
    w = Wire()
    set_trace_processors([LangfuseOtelTracingProcessor(w.client)])
    yield w
    set_trace_processors([])


def run_flow(metadata=None):
    with trace(
        "my-flow",
        trace_id=TRACE_ID,
        group_id="sess-1",
        metadata=metadata
        if metadata is not None
        else {
            "tenant": "t-1",
            "langfuse.user_id": "user-4",
            "langfuse.tags": "one, two,,three",
            "langfuse.version": "9",
            "langfuse.release": "rel-9",
        },
    ) as t:
        with agent_span(name="assistant", tools=["read"]):
            with generation_span(
                input=[{"role": "user", "content": "hi"}],
                output=[{"role": "assistant", "content": "hello"}],
                model="gpt-5.1",
                usage={"input_tokens": 3, "output_tokens": 2},
            ):
                pass
            with function_span(name="read", input='{"id": 1}', output="ok"):
                pass
            with custom_span(name="step", data={"a": 1}) as s:
                s.set_error({"message": "boom", "data": {"x": 1}})
    return t


def test_a_run_is_one_trace_with_the_sdk_id_and_the_sdk_nesting(wire):
    t = run_flow()
    spans = wire.spans()
    assert len(spans) == 5
    trace_hex = langfuse_id(t.trace_id)
    assert {format(s.context.trace_id, "032x") for s in spans} == {trace_hex}

    root = wire.named("my-flow")
    agent = wire.named("agent:assistant")
    [generation] = wire.by_type("generation")
    tool = wire.named("function:read")
    assert agent.parent.span_id == root.context.span_id
    assert generation.parent.span_id == agent.context.span_id
    assert tool.parent.span_id == agent.context.span_id
    assert root.attributes.get("langfuse.internal.as_root") is True
    assert "langfuse.internal.as_root" not in agent.attributes
    assert generation.start_time <= generation.end_time


def test_the_observation_types_follow_the_span_kinds(wire):
    run_flow()
    types = {
        s.name: s.attributes.get("langfuse.observation.type") for s in wire.spans()
    }
    assert types["agent:assistant"] == "agent"
    assert types["function:read"] == "tool"
    assert types["custom:step"] == "span"
    assert types["my-flow"] == "span"
    assert len(wire.by_type("generation")) == 1


def test_every_observation_carries_the_traces_user_session_tags_version_and_metadata(
    wire,
):
    run_flow()
    for span in wire.spans():
        assert span.attributes.get("user.id") == "user-4"
        assert span.attributes.get("session.id") == "sess-1"
        assert tuple(span.attributes.get("langfuse.trace.tags")) == (
            "one",
            "two",
            "three",
        )
        assert span.attributes.get("langfuse.version") == "9"
        assert span.attributes.get("langfuse.trace.name") == "my-flow"
        assert attribute(span, "langfuse.trace.metadata.tenant") == "t-1"
        # The release rides the metadata; the reserved keys are not forwarded.
        assert attribute(span, "langfuse.trace.metadata.asas_release") == "rel-9"
        assert not any("langfuse.user_id" in key for key in span.attributes)


def test_a_generation_keeps_the_model_the_messages_and_the_usage(wire):
    run_flow()
    [generation] = wire.by_type("generation")
    assert generation.attributes.get("langfuse.observation.model.name") == "gpt-5.1"
    assert attribute(generation, "langfuse.observation.input") == [
        {"role": "user", "content": "hi"}
    ]
    assert attribute(generation, "langfuse.observation.output") == [
        {"role": "assistant", "content": "hello"}
    ]
    assert attribute(generation, "langfuse.observation.usage_details") == {
        "input_tokens": 3,
        "output_tokens": 2,
    }


def test_a_tool_keeps_its_arguments_and_result_and_an_error_is_the_level(wire):
    run_flow()
    tool = wire.named("function:read")
    assert attribute(tool, "langfuse.observation.input") in ('{"id": 1}', {"id": 1})
    assert attribute(tool, "langfuse.observation.output") == "ok"
    assert tool.attributes.get("langfuse.observation.level") != "ERROR"
    step = wire.named("custom:step")
    assert attribute(step, "langfuse.observation.input") == {"data": {"a": 1}}
    assert step.attributes.get("langfuse.observation.level") == "ERROR"
    assert step.attributes.get("langfuse.observation.status_message") == "boom"


def test_the_mask_applies_and_a_raising_mask_never_sends_the_value(caplog):
    w = Wire()

    def mask(*, data):
        raise ValueError(f"cannot mask {data!r}")

    set_trace_processors([LangfuseOtelTracingProcessor(w.client, mask=mask)])
    try:
        run_flow()
    finally:
        set_trace_processors([])
    [generation] = w.by_type("generation")
    assert attribute(generation, "langfuse.observation.input") == MASK_FAILED
    assert attribute(generation, "langfuse.observation.output") == MASK_FAILED
    assert "hello" not in repr([dict(s.attributes) for s in w.spans()])
    failures = [r for r in caplog.records if "mask failed" in r.getMessage()]
    assert failures and all(
        "hello" not in r.getMessage() and r.exc_info is None for r in failures
    )


def test_a_span_of_the_hosts_receives_none_of_the_traces_attributes(wire):
    host_exporter = InMemorySpanExporter()
    host = TracerProvider()
    host.add_span_processor(SimpleSpanProcessor(host_exporter))
    with host.get_tracer("host").start_as_current_span("GET /candidates"):
        run_flow()
    [request] = host_exporter.get_finished_spans()
    assert not any(
        key.startswith(("user.", "session.", "langfuse.")) for key in request.attributes
    )
    # ... and the run's trace is still its own, not the request's.
    assert {format(s.context.trace_id, "032x") for s in wire.spans()} == {"c" * 32}


def test_run_agent_links_the_generation_to_the_prompt_version_and_names_the_model(
    wire,
):
    run(
        run_agent(
            snapshot(source=SOURCE),
            no_tools,
            never_asked,
            renderer=RENDERER,
            run_model=SpanOpeningModel("response"),
        )
    )
    [generation] = wire.by_type("generation")
    assert (
        generation.attributes.get("langfuse.observation.prompt.name") == "agent-support"
    )
    assert generation.attributes.get("langfuse.observation.prompt.version") == 7


class Broken:
    def start_observation(self, **kw):
        raise RuntimeError("langfuse down")

    def flush(self):
        raise RuntimeError("langfuse down")


def test_a_failing_client_never_fails_the_run(caplog):
    processor = LangfuseOtelTracingProcessor(Broken(), flush_on_trace_end=True)
    set_trace_processors([processor])
    try:
        run_flow()  # raises nothing
    finally:
        set_trace_processors([])
    processor.shutdown()
    processor.shutdown()
    assert any("could not open trace" in r.getMessage() for r in caplog.records)


def test_a_trace_without_metadata_still_has_its_name_and_session(wire):
    run_flow(metadata={})
    root = wire.named("my-flow")
    assert root.attributes.get("session.id") == "sess-1"
    assert "user.id" not in root.attributes


def test_import_asas_agent_does_not_load_langfuse_or_opentelemetry():
    import subprocess
    import sys
    from pathlib import Path

    src = str(Path(__file__).resolve().parents[1] / "src")
    code = (
        "import sys, asas_agent; "
        "print(any(m.startswith(('langfuse', 'opentelemetry')) for m in sys.modules))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        env={"PYTHONPATH": src, "PATH": ""},
    )
    assert out.stdout.strip() == "False"


def test_the_release_store_calls_fit_the_v3_and_later_client():
    # LangfuseStore is the same for both extras: every call it makes binds to the
    # v3+ client's own signatures (get_prompt, create_prompt, update_prompt).
    import inspect

    client = Wire().client
    inspect.signature(client.get_prompt).bind(
        "agent-x", label="dev", type="text", cache_ttl_seconds=60
    )
    inspect.signature(client.create_prompt).bind(
        name="agent-x",
        prompt="text",
        config={},
        labels=["release-0123"],
        type="text",
        commit_message="release 0123",
    )
    inspect.signature(client.update_prompt).bind(
        name="agent-x", version=3, new_labels=["dev", "release-0123"]
    )
