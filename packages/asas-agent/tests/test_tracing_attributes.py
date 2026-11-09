"""0.5.0: a run tells Langfuse its user, tags, version and release, whether
`run_agent` opened the SDK trace or the host did; a release read from a Langfuse
prompt links its generations to that version; a generation the Responses API left
without a model gets the run's; shutdown flushes once and never raises."""

import dataclasses
from types import SimpleNamespace

import pytest
from agents import set_trace_processors
from agents.models.interface import Model
from agents.tracing import generation_span, response_span, trace
from conftest import run
from test_loop import RENDERER, ScriptedModel, never_asked
from test_loop import snapshot as plain_snapshot
from test_tracing import Client

from asas_agent import TraceAttributes, run_agent
from asas_agent.trace_attributes import reserved_metadata
from asas_agent.tracing import LangfuseTracingProcessor

SOURCE = {"name": "agent-support", "version": 7, "label": "production"}


def snapshot(source=None, tracing=None):
    shot = plain_snapshot()
    release = {
        **shot["release"],
        "id": "r-1",
        "settings": {"tracing": tracing or {"enabled": True}},
    }
    if source:
        release["source"] = source
    shot["release"] = release
    return shot


class SpanOpeningModel(ScriptedModel):
    """Opens the span a real model opens: a `generation` (chat completions) or a
    `response` (the Responses API, which exports no model)."""

    def __init__(self, kind, model=None):
        super().__init__([(None, "Done.")])
        self.kind, self.named = kind, model

    async def get_response(self, *args, **kwargs):
        if self.kind == "generation":
            span = generation_span(input=[{"role": "user"}], model=self.named)
        else:
            span = response_span()
        with span:
            return await super().get_response(*args, **kwargs)


async def no_tools(*args):
    raise AssertionError("no tool was expected")


@pytest.fixture
def client():
    c = Client()
    set_trace_processors([LangfuseTracingProcessor(c)])
    yield c
    set_trace_processors([])


def run_it(shot, run_model, **kwargs):
    return run(
        run_agent(
            shot,
            no_tools,
            never_asked,
            renderer=RENDERER,
            run_model=run_model,
            **kwargs,
        )
    )


def opened(client):
    [call] = [kw for kind, kw in client.calls if kind == "trace"]
    return call


def generations(client):
    return [kw for kind, kw in client.calls if kind == "generation"]


# -- trace attributes -------------------------------------------------------------


def test_trace_attributes_are_a_frozen_value_with_no_commas_in_tags():
    attributes = TraceAttributes(user_id="u-1", tags=("a", "b"))
    assert attributes.metadata == {} and attributes.version is None
    with pytest.raises(dataclasses.FrozenInstanceError):
        attributes.user_id = "u-2"  # type: ignore[misc]
    with pytest.raises(ValueError, match="comma"):
        TraceAttributes(tags=("a,b",))
    with pytest.raises(TypeError):
        TraceAttributes(tags="a")  # type: ignore[arg-type]
    assert reserved_metadata(None) == {} and reserved_metadata(TraceAttributes()) == {}
    assert reserved_metadata(
        TraceAttributes(user_id="u", tags=("a", "b"), version="v", release="r")
    ) == {
        "langfuse.user_id": "u",
        "langfuse.tags": "a,b",
        "langfuse.version": "v",
        "langfuse.release": "r",
    }


def test_run_agent_sends_the_attributes_on_the_langfuse_trace(client):
    attributes = TraceAttributes(
        user_id="user-9",
        session_id="sess-attr",
        tags=("screening", "qa"),
        version="3",
        release="2026.11.0",
        metadata={"tenant": "t-1", "from": "attributes"},
    )
    run_it(
        snapshot(),
        SpanOpeningModel("generation", "m-1"),
        trace=attributes,
        trace_metadata={"from": "trace_metadata"},
    )
    call = opened(client)
    assert call["user_id"] == "user-9" and call["tags"] == ["screening", "qa"]
    assert call["version"] == "3" and call["release"] == "2026.11.0"
    assert call["session_id"] == "sess-attr"  # no group_id was given
    assert call["metadata"] == {
        "tenant": "t-1",
        "from": "trace_metadata",  # trace_metadata wins key by key
        "release_id": "r-1",
    }


def test_group_id_wins_over_the_attributes_session(client):
    run_it(
        snapshot(),
        SpanOpeningModel("generation", "m-1"),
        group_id="sess-group",
        trace=TraceAttributes(session_id="sess-attr"),
    )
    assert opened(client)["session_id"] == "sess-group"


def test_without_attributes_the_trace_is_what_it_was(client):
    run_it(snapshot(), SpanOpeningModel("generation", "m-1"), group_id="s")
    call = opened(client)
    assert {"user_id", "tags", "version", "release"}.isdisjoint(call)
    assert call["metadata"] == {"release_id": "r-1"}


def test_a_host_trace_carries_the_attributes_in_reserved_metadata_keys(client):
    # The host owns the trace (its id, its name); run_agent joins it, so the
    # attributes can only travel in the metadata the host gives its trace.
    with trace(
        "host-flow",
        trace_id="trace_" + "b" * 32,
        group_id="sess-host",
        metadata={
            "tenant": "t-2",
            "langfuse.user_id": "user-4",
            "langfuse.tags": "one, two,,three",
            "langfuse.version": "9",
            "langfuse.release": "rel-9",
        },
    ):
        run_it(snapshot(), SpanOpeningModel("generation", "m-1"))
    call = opened(client)
    assert call["id"] == "b" * 32 and call["name"] == "host-flow"
    assert call["user_id"] == "user-4" and call["version"] == "9"
    assert call["tags"] == ["one", "two", "three"] and call["release"] == "rel-9"
    assert call["session_id"] == "sess-host"
    assert call["metadata"] == {"tenant": "t-2"}  # reserved keys are stripped


def test_a_host_trace_with_only_reserved_keys_forwards_no_metadata(client):
    with trace("host-flow", metadata={"langfuse.user_id": "u"}):
        pass
    call = opened(client)
    assert call["user_id"] == "u" and call["metadata"] is None


# -- the prompt link --------------------------------------------------------------


def test_every_generation_of_a_langfuse_release_links_to_its_prompt_version(client):
    run_it(snapshot(source=SOURCE), SpanOpeningModel("generation", "m-1"))
    [generation] = generations(client)
    assert (generation["prompt"].name, generation["prompt"].version) == (
        "agent-support",
        7,
    )
    assert generation["prompt"].is_fallback is False


def test_a_release_without_a_source_links_nothing(client):
    run_it(snapshot(), SpanOpeningModel("generation", "m-1"))
    [generation] = generations(client)
    assert "prompt" not in generation


def test_the_link_reaches_a_run_inside_a_host_trace(client):
    with trace("host-flow"):
        run_it(snapshot(source=SOURCE), SpanOpeningModel("response"))
    [generation] = generations(client)
    assert generation["prompt"].version == 7


def test_the_link_is_not_left_behind_after_the_run(client):
    run_it(snapshot(source=SOURCE), SpanOpeningModel("generation", "m-1"))
    with trace("later"), generation_span(model=None):
        pass
    later = generations(client)[-1]
    assert "prompt" not in later and later["model"] is None


def test_the_langfuse_2x_client_takes_the_trace_fields_and_the_link():
    # Against the real 2.x client, its queue stubbed: the trace and the generation
    # bodies the SDK would send.
    langfuse = pytest.importorskip("langfuse")
    from unittest.mock import MagicMock

    real = langfuse.Langfuse(
        public_key="pk", secret_key="sk", host="http://localhost:1", release="env"
    )
    queue = MagicMock()
    real.task_manager = queue
    set_trace_processors([LangfuseTracingProcessor(real)])
    try:
        run_it(
            snapshot(source=SOURCE),
            SpanOpeningModel("response"),
            trace=TraceAttributes(
                user_id="u-1", tags=("t",), version="4", release="rel-1"
            ),
        )
    finally:
        set_trace_processors([])
    bodies = [c.args[0]["body"] for c in queue.add_task.call_args_list]
    [t] = [b for b in bodies if type(b).__name__ == "TraceBody"]
    assert (t.user_id, t.tags, t.version, t.release) == ("u-1", ["t"], "4", "rel-1")
    [g] = [b for b in bodies if type(b).__name__ == "CreateGenerationBody"]
    assert (g.prompt_name, g.prompt_version) == ("agent-support", 7)
    assert g.model == "unused"  # the response span had none: the release's model


# -- the model on a generation ----------------------------------------------------


def test_a_generation_without_a_model_gets_the_runs_in_both_span_kinds(client):
    run_it(snapshot(), SpanOpeningModel("generation"))
    run_it(snapshot(), SpanOpeningModel("response"))
    assert [g["model"] for g in generations(client)] == ["unused", "unused"]


def test_the_run_model_then_the_model_name_then_the_releases(client):
    class Named(SpanOpeningModel):
        model = "gpt-5.1"

    run_it(snapshot(), Named("response"))
    run_it(snapshot(), SpanOpeningModel("response"), model="gpt-4o")
    assert [g["model"] for g in generations(client)] == ["gpt-5.1", "gpt-4o"]


def test_a_span_that_names_its_model_keeps_it(client):
    run_it(snapshot(), SpanOpeningModel("generation", "served-model"))
    [generation] = generations(client)
    assert generation["model"] == "served-model"


def test_the_response_objects_model_is_what_the_provider_served(client):
    with trace("t"):
        with response_span(response=SimpleNamespace(id="resp_1", model="gpt-5-0801")):
            pass
    assert generations(client)[0]["model"] == "gpt-5-0801"


def test_outside_a_run_a_modelless_generation_stays_modelless(client):
    with trace("t"), generation_span(model=None):
        pass
    assert generations(client)[0]["model"] is None


# -- flushing ---------------------------------------------------------------------


def test_shutdown_flushes_once_and_never_raises():
    c = Client()
    processor = LangfuseTracingProcessor(c)
    processor.shutdown()
    processor.shutdown()
    assert c.flushes == 1
    processor.force_flush()
    assert c.flushes == 2

    class Broken(Client):
        def flush(self):
            raise RuntimeError("down")

    broken = LangfuseTracingProcessor(Broken())
    broken.force_flush()
    broken.shutdown()
    broken.shutdown()


def test_the_model_protocol_is_unchanged_by_the_test_double():
    assert issubclass(SpanOpeningModel, Model)
