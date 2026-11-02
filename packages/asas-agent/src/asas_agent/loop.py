"""The SDK owns the loop; the host fills the seams (:mod:`asas_agent.seams`).

The agent runs the conversation; the host owns the gates. A skill is a playbook the
agent reads: `activate_skill` swaps the instructions and the tools on the next model
step and verifies nothing. State lives in the host's record, which opens the input
and comes back with every tool result that carries it.

One run spans the conversation: the agent speaks to the person through the host's
ask tool, whose handler returns their answer when it arrives; a plain reply ends the
run. The handler may instead raise `AskPending` when nobody in this process can
wait: the run ends at the question and the next message starts a fresh run.

Imports the OpenAI Agents SDK, so `import asas_agent` loads this module only when one
of its names is first used. Nothing here reads the clock, the environment or a
global, so a host may run the loop inside a workflow sandbox.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from agents import (
    Agent,
    FunctionTool,
    Model,
    ModelProvider,
    ModelSettings,
    RunConfig,
    Runner,
)
from agents.exceptions import AgentsException
from agents.tool import set_function_tool_failure_error_function

from .models import usage_of
from .seams import AskHandler, ContextRenderer, SkillAware, ToolDispatcher


class AskPending(AgentsException):
    """The question is recorded and nothing in this process waits for the answer.
    An AgentsException leaves the SDK as it is; any other error a tool raises is
    wrapped into a UserError."""


class ToolBudgetExceeded(AgentsException):
    """The release's max_tools reached: the run stops rather than loops."""


@dataclass
class SkillState:
    active: str | None = None
    calls: int = 0
    record: str = ""
    trace: list[dict] = field(default_factory=list)


def failure_tool_trace(error: Exception) -> list[dict]:
    """Extract only the loop's typed tool observations, excluding SDK/provider messages."""
    if isinstance(error, AgentsException) and error.run_data:
        state = error.run_data.context_wrapper.context
        if isinstance(state, SkillState):
            return list(state.trace)
    return []


def empty_usage() -> dict:
    return {
        "model_calls": 0,
        "input_tokens": 0,
        "cached_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "calls": [],
    }


def instructions_for(
    snapshot: Mapping[str, Any], active_skill: str | None, renderer: ContextRenderer
) -> str:
    """What every call shares, so the provider's prompt cache holds it: the prompt, the
    host's head sections, the skill catalogue, the active skill's playbook (it changes
    only at activate_skill) and the host's tail sections. The record is not here: it
    rides in the input, opened by the first item and returned by every write, so a
    write invalidates nothing before it. Pure, so a model called by reference renders
    the same text from the run's snapshot."""
    release = snapshot["release"]
    skills = release["skills"]
    catalogue = "\n".join(f"- {key}: {s['description']}" for key, s in skills.items())
    if active_skill and active_skill in skills:
        skill = skills[active_skill]
        active = f"Skill {active_skill} is active. Follow these instructions.\n\n{skill['instructions']}"
        clauses = skill.get("clauses_text") or ""
    else:
        active = renderer.no_skill
        clauses = ""
    parts = [release["instructions"], *renderer.head(snapshot)]
    if skills:
        # A release with no skill has no catalogue to read and no skill to activate,
        # so neither section is rendered (nor is `activate_skill` offered).
        parts += [f"## Skills\n{catalogue}", f"## Active skill\n{active}"]
    if clauses:
        parts.append(f"## The policy clauses this skill applies\n{clauses}")
    parts.extend(renderer.tail(snapshot))
    return "\n\n".join(parts)


def activate_skill_tool(renderer: ContextRenderer) -> dict:
    return {
        "name": "activate_skill",
        "description": renderer.activate_skill,
        "schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
    }


def described_tools(
    snapshot: Mapping[str, Any], renderer: ContextRenderer
) -> list[dict]:
    """Every tool the model sees, activate_skill first, each description prefixed with
    the skills that hold it. Pure, so a model called by reference builds the same list.

    A release with no skill is not offered `activate_skill`: there is nothing it could
    activate, and a one-shot release then calls the model with no tool at all, which a
    model that refuses a reasoning effort beside a function tool on chat completions
    accepts (and every call is the tool's description shorter)."""
    skills = snapshot["release"]["skills"]

    def described(d: dict) -> str:
        holders = sorted(k for k, s in skills.items() if d["name"] in s["tools"])
        if d["name"] in renderer.always or not holders:
            return d["description"]
        return f"[{', '.join(holders)}] " + d["description"]

    host = [{**d, "description": described(d)} for d in snapshot["tools"]]
    return [activate_skill_tool(renderer), *host] if skills else host


def model_settings(
    conversation_id: str | None = None, release: Mapping[str, Any] | None = None
) -> ModelSettings:
    """Without a provider-side conversation nothing is stored there: the transcript,
    reasoning items included, is the host's, and encrypted reasoning is what lets the
    next call continue the plan. Several tools in one model call: the ones the host
    declares read-only run together, every other one in its turn (`run_agent`).
    The release's `settings.model` (temperature, reasoning, max_tokens...) lays over
    these; an unknown name is refused by the SDK."""
    base = ModelSettings(
        parallel_tool_calls=True,
        store=bool(conversation_id),
        response_include=None if conversation_id else ["reasoning.encrypted_content"],
    )
    configured = ((release or {}).get("settings") or {}).get("model")
    return base.resolve(ModelSettings(**configured)) if configured else base


async def run_agent(
    snapshot: Mapping[str, Any],
    dispatch: ToolDispatcher,
    ask: AskHandler,
    *,
    renderer: ContextRenderer,
    model: Model | str | None = None,
    provider: ModelProvider | None = None,
    run_model: Model | None = None,
    trace_sink: Callable[[dict], None] | None = None,
    conversation_id: str | None = None,
    output_type: type[Any] | None = None,
    group_id: str | None = None,
    trace_metadata: Mapping[str, Any] | None = None,
) -> dict:
    """One run of the release in `snapshot` (`release`, `tools`, `history`, and
    optionally `active_skill` and `record`) until the agent answers, the ask handler
    raises `AskPending`, or an `AgentsException` ends it. Returns the answer, the new
    history, the active skill, the tool trace, the record and the usage.

    The calls of one model answer start in the order the model issued them. A tool
    whose descriptor in `tools` says `"read_only": true` runs beside the calls around
    it; any other tool (activate_skill and the ask tool included) waits for every call
    before it and holds back every call after it, so a write never races a read of the
    same record. Indexes are given at the start and the trace is kept in that order, so
    a host replaying the run sees the same sequence.

    With `output_type` (a pydantic model, a dataclass or a TypedDict) the agent's
    final answer is that type, validated by the SDK against its strict schema, and
    the result carries it as `output`; `answer` stays the text, empty for a typed
    answer. Without it `output` is None.

    Tracing is the release's: `settings.tracing.enabled` turns the SDK's trace on for
    this run (named after the release, `release_id` in its metadata beside the host's
    `trace_metadata`, in the `group_id` session), and `settings.tracing.sensitive`
    keeps the run's content in it. Off unless the release says so; where the trace
    goes is the host's `set_trace_processors`."""
    release = snapshot["release"]
    skills = release["skills"]
    state = SkillState(
        snapshot.get("active_skill"), record=snapshot.get("record") or ""
    )
    read_only = frozenset(d["name"] for d in snapshot["tools"] if d.get("read_only"))
    # The last call that runs alone, and the read-only calls started since it.
    alone: asyncio.Event | None = None
    beside: list[asyncio.Event] = []
    # Trace events wait here until every call before theirs is traced or has ended.
    held: dict[int, dict | None] = {}
    traced = 0
    if state.active not in skills:
        state.active = None

    def instructions(_ctx, _agent):
        return instructions_for(snapshot, state.active, renderer)

    def enabled(name):
        return (
            name == "activate_skill"
            or name in renderer.always
            or (state.active in skills and name in skills[state.active]["tools"])
        )

    def observed(event: dict) -> None:
        held[event["index"]] = event
        release_trace()

    def ended(index: int) -> None:
        held.setdefault(index, None)
        release_trace()

    def release_trace() -> None:
        nonlocal traced
        while traced + 1 in held:
            traced += 1
            event = held.pop(traced)
            if event is not None:
                state.trace.append(event)
                if trace_sink is not None:
                    trace_sink(event)

    def make_tool(name: str, description: str, schema: dict):
        async def invoke(ctx, arguments):
            nonlocal alone, beside
            # Nothing is awaited before the call takes its index and its place.
            state.calls += 1
            index = state.calls
            done = asyncio.Event()
            waits = [alone] if alone else []
            if name in read_only:
                beside = [*(e for e in beside if not e.is_set()), done]
            else:
                waits += beside
                alone, beside = done, []
            try:
                if index > release["max_tools"]:
                    raise ToolBudgetExceeded("agent_tool_budget")
                for event in waits:
                    await event.wait()
                return await invoke_in_turn(ctx, arguments, index)
            finally:
                done.set()
                ended(index)

        async def invoke_in_turn(ctx, arguments, index):
            if not enabled(name):
                holders = sorted(k for k, s in skills.items() if name in s["tools"])
                observed(
                    {
                        "index": index,
                        "call_id": ctx.tool_call_id,
                        "name": name,
                        "skill_before": state.active,
                        "skill_after": state.active,
                        "arguments": json.loads(arguments) if arguments else {},
                        "ok": False,
                        "code": "denied",
                        "version": None,
                    }
                )
                return json.dumps(
                    {
                        "ok": False,
                        "code": "denied",
                        "message": f"{name} is not a tool of the active skill"
                        + (
                            f"; activate {' or '.join(holders)} first."
                            if holders
                            else "."
                        ),
                    }
                )
            args = json.loads(arguments) if arguments else {}
            before = state.active
            if name == "activate_skill":
                if args.get("name") not in skills:
                    result = {
                        "ok": False,
                        "code": "unknown_skill",
                        "skills": sorted(skills),
                    }
                else:
                    state.active = args["name"]
                    result = {
                        "ok": True,
                        "active_skill": state.active,
                        "tools": sorted(skills[state.active]["tools"]),
                    }
            elif name == renderer.ask_tool:
                # The handler records the question and returns the answer when it comes;
                # AskPending ends the run at the question, traced.
                try:
                    result = await ask(ctx.tool_call_id, args)
                except AskPending:
                    observed(
                        {
                            "index": index,
                            "call_id": ctx.tool_call_id,
                            "name": name,
                            "skill_before": before,
                            "skill_after": state.active,
                            "arguments": args,
                            "ok": True,
                            "code": "asked",
                            "version": None,
                        }
                    )
                    raise
            else:
                try:
                    result = await dispatch(ctx.tool_call_id, name, args, state.active)
                except AgentsException:
                    raise  # the dispatcher ends the run, never a refusal
                # A failed tool is a typed refusal the model reads.
                except Exception as error:  # noqa: BLE001
                    result = {
                        "ok": False,
                        "code": "tool_failed",
                        "message": f"{name} could not be run ({type(error).__name__}); "
                        "say so if it blocks you, or go on without it.",
                    }
                record = renderer.record(result)
                if record:
                    state.record = record
            observed(
                {
                    "index": index,
                    "call_id": ctx.tool_call_id,
                    "name": name,
                    "skill_before": before,
                    "skill_after": state.active,
                    "arguments": args,
                    "ok": bool(result.get("ok")),
                    "code": result.get("code"),
                    # The record's revision, when the result carries one.
                    "version": (result.get("data") or {}).get("revision")
                    if isinstance(result.get("data"), dict)
                    else None,
                }
            )
            return json.dumps(result, default=str)

        tool = FunctionTool(
            name=name,
            description=description,
            params_json_schema=schema,
            on_invoke_tool=invoke,
            # Every tool stays visible: one the active skill does not hold is refused with
            # the skill to activate, instead of ending the run (ModelBehaviorError).
            is_enabled=True,
        )
        # No formatter turns an exception into a message for the model: a failed tool is
        # already a typed refusal above, and AskPending or the budget must end the run.
        return set_function_tool_failure_error_function(tool, None)

    tools: list[Any] = [
        make_tool(d["name"], d["description"], d["schema"])
        for d in described_tools(snapshot, renderer)
    ]
    if isinstance(run_model, SkillAware):
        # A model by reference is sent the active skill's name, not the rendered text.
        run_model.bind(lambda: state.active)
    agent = Agent(
        name=release["name"],
        instructions=instructions,
        tools=tools,
        model=model or release["model"],
        model_settings=model_settings(conversation_id, release),
        output_type=output_type,
    )
    chosen: dict[str, Any] = {"model": run_model}
    if provider:
        chosen["model_provider"] = provider
    tracing = (release.get("settings") or {}).get("tracing") or {}
    config = RunConfig(
        tracing_disabled=not tracing.get("enabled", False),
        trace_include_sensitive_data=bool(tracing.get("sensitive", False)),
        workflow_name=release["name"],
        group_id=group_id,
        trace_metadata={
            **(trace_metadata or {}),
            "release_id": release.get("id", ""),
        },
        **chosen,
    )
    try:
        result = await Runner.run(
            agent,
            input=list[Any]([*renderer.opening(state.record), *snapshot["history"]]),
            context=state,
            max_turns=release["max_turns"],
            run_config=config,
            conversation_id=conversation_id,
        )
    except AskPending:
        return {
            "answer": "",
            "history": [],
            "active_skill": state.active,
            "tool_trace": state.trace,
            "record": state.record,
            "usage": empty_usage(),
            "ended": "asked",
            "output": None,
        }
    answer = result.final_output
    return {
        "answer": answer.strip() if isinstance(answer, str) else "",
        "history": result.to_input_list(),
        "active_skill": state.active,
        "tool_trace": state.trace,
        "record": state.record,
        "usage": run_usage(result),
        "ended": "answered",
        "output": answer if output_type is not None else None,
    }


def run_usage(result: Any) -> dict:
    """The run's tokens as the provider counted them: the total and one entry per model
    call (input, of which cached; output, of which reasoning). Zeros from a model that
    counts nothing, such as a scripted one."""
    calls = [
        usage_of(getattr(response, "usage", None))
        for response in getattr(result, "raw_responses", None) or []
    ]
    total = {
        k: sum(c[k] for c in calls)
        for k in ("input_tokens", "cached_tokens", "output_tokens", "reasoning_tokens")
    }
    return {"model_calls": len(calls), **total, "calls": calls}
