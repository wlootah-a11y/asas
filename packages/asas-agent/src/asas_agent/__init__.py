"""Asas Agent: the framework that runs agents, on the OpenAI Agents SDK.

* **Releases.** An agent's prompt, skills, model, budgets and settings captured whole
  under the hash of their content (:func:`build_release`, :func:`load_release`); a run
  pins its release and never rereads it, so retries and replays see one agent. A
  release lives in a store the host chooses (:class:`ReleaseStore`: :class:`FileStore`,
  :class:`LangfuseStore` or its own), read through a label (:func:`fetch_release`).
* **Skills.** Playbooks in SKILL.md files with a YAML header (:mod:`asas_agent.skills`),
  activated by the agent; one stage of a case sees only its own (:func:`stage_view`).
* **Run contracts.** The payloads between a host's orchestration and a run
  (:class:`RunInput`, :class:`ToolCall`, :class:`ModelCall`, :class:`Ask`,
  :class:`Delivery`, :class:`TurnResult`, :class:`Failure`), stable under replay.
* **The SDK loop.** :func:`run_agent` runs a release's skills on the OpenAI Agents SDK
  until the agent answers or waits; the host fills its seams (:mod:`asas_agent.seams`):
  the tool dispatcher, the ask handler, the model and the context renderer that gives
  the agent its record and the host's words.
* **Evals.** Scenario suites run four times and judged on what was observed
  (:func:`run_suite`, :func:`summarize_repetitions`, :func:`assert_expected_record`),
  on a measured gateway (:class:`GatewayModels`, :class:`MeasuredModel`); the
  ``asas-agent eval`` command runs a suite.

Independent of Temporal: a host that keeps runs in durable workflows plugs into the
seams (DESIGN.md), and every module is safe to import inside a workflow sandbox: no
I/O, randomness or global state at import. ``import asas_agent`` loads neither the
Agents SDK nor the OpenAI client; :mod:`asas_agent.models` and :mod:`asas_agent.loop`
load them when one of their names is first used.

Host contract shape: **table-less, router-less** in this release.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .contracts import Ask, Delivery, Failure, ModelCall, RunInput, ToolCall, TurnResult
from .evaluation import (
    ProcedureFailure,
    ScenarioStep,
    assert_expected_record,
    parse_scenario,
    run_suite,
    summarize_repetitions,
)
from .releases import (
    FileStore,
    build_release,
    fetch_release,
    load_release,
    read_release,
    release_from,
    release_id,
    stage_tools,
    stage_view,
)
from .seams import AskHandler, ContextRenderer, ReleaseStore, SkillAware, ToolDispatcher
from .skills import load_skills, parse_skill
from .trace_attributes import TraceAttributes

if TYPE_CHECKING:  # pragma: no cover
    from .guard import GuardedModel
    from .langfuse_store import LangfuseStore
    from .loop import (
        AskPending,
        SkillState,
        ToolBudgetExceeded,
        described_tools,
        empty_usage,
        failure_tool_trace,
        instructions_for,
        model_settings,
        run_agent,
        run_usage,
    )
    from .models import GATEWAY_KEYS, GatewayModels, MeasuredModel, usage_of
    from .providers import azure_provider, gateway_provider
    from .tracing import LangfuseTracingProcessor

__version__ = "0.5.0"

#: Names served by the modules that import the SDK, loaded on first use: the eval
#: models, the production providers, the Langfuse tracing, the guarded model and the
#: SDK loop.
_LAZY: dict[str, str] = {
    "GATEWAY_KEYS": "models",
    "GatewayModels": "models",
    "MeasuredModel": "models",
    "usage_of": "models",
    "gateway_provider": "providers",
    "azure_provider": "providers",
    "LangfuseTracingProcessor": "tracing",
    "GuardedModel": "guard",
    "LangfuseStore": "langfuse_store",
    "AskPending": "loop",
    "SkillState": "loop",
    "ToolBudgetExceeded": "loop",
    "described_tools": "loop",
    "empty_usage": "loop",
    "failure_tool_trace": "loop",
    "instructions_for": "loop",
    "model_settings": "loop",
    "run_agent": "loop",
    "run_usage": "loop",
}


def __getattr__(name: str) -> Any:
    module_name = _LAZY.get(name)
    if module_name is not None:
        from importlib import import_module

        return getattr(import_module(f".{module_name}", __name__), name)
    raise AttributeError(f"module 'asas_agent' has no attribute {name!r}")


__all__ = [
    "GATEWAY_KEYS",
    "Ask",
    "AskHandler",
    "AskPending",
    "ContextRenderer",
    "Delivery",
    "Failure",
    "FileStore",
    "GatewayModels",
    "GuardedModel",
    "LangfuseStore",
    "LangfuseTracingProcessor",
    "MeasuredModel",
    "ModelCall",
    "ProcedureFailure",
    "ReleaseStore",
    "RunInput",
    "ScenarioStep",
    "SkillAware",
    "SkillState",
    "ToolBudgetExceeded",
    "ToolCall",
    "ToolDispatcher",
    "TraceAttributes",
    "TurnResult",
    "__version__",
    "assert_expected_record",
    "azure_provider",
    "build_release",
    "described_tools",
    "empty_usage",
    "failure_tool_trace",
    "fetch_release",
    "gateway_provider",
    "instructions_for",
    "load_release",
    "load_skills",
    "model_settings",
    "parse_scenario",
    "parse_skill",
    "read_release",
    "release_from",
    "release_id",
    "run_agent",
    "run_suite",
    "run_usage",
    "stage_tools",
    "stage_view",
    "summarize_repetitions",
    "usage_of",
]
