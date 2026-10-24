"""Asas LLM — the shared way to call a language model.

Three things every AI product rebuilds, done once:

* **Prompts as data.** A prompt lives in a registry (Langfuse) where it is
  edited without a deploy, and every read goes **registry, then last-good
  copy, then a local file shipped with the image**
  (:class:`~asas_llm.prompts.LayeredPromptStore`). A replica that starts while
  the registry is unreachable still answers; the registry is where prompts are
  *edited*, not what the process *needs to exist*. :mod:`asas_llm.snapshot`
  keeps the local copy current.
* **Every call traced, grouped per request, and the id handed back.** The host
  binds one session per inbound request (:func:`bind_request`, or the
  :class:`~asas_llm.middleware.TraceIdMiddleware` from a header); every trace
  joins that session; :func:`trace_ids` reads them back for a response header,
  a body field or a log line. The id exists before the tracing backend is
  asked, so it is on every result and every error whether or not the backend
  answered.
* **Structured output that does not come back broken.** Ask the provider for
  strict JSON matching the pydantic model, repair what comes back before
  parsing it, and when it still does not validate raise with the raw text
  attached (:mod:`asas_llm.structured`).

Host contract shape: **table-less, router-less** — the variant
``asas-storage`` and ``asas-ratelimit`` use. No ``migrate``, no ``seed``, no
``build_routers``. One hook, :func:`configure`, and a process-wide
:func:`runner` built lazily from it, mirroring ``asas_storage.configure`` /
``asas_storage.storage()``.

And the gateway layers the same engine had to build in front of its calls,
each a seam the host fills from its own settings:

* **The guard** (:mod:`asas_llm.guard`, :mod:`asas_llm.rules`,
  :mod:`asas_llm.content_safety`): every chat completion screened for prompt
  injection before it is sent and for unsafe content before its answer is
  used, by built-in rules plus a scanner (Azure AI Content Safety ships); a
  finding is logged, counted and, when the host enforces, refused.
* **Admission** (:mod:`asas_llm.admission`, :mod:`asas_llm.slots`): one cap on
  model requests in flight across every process and replica, in Redis, with
  an interactive lane that bulk work gives way to.
* **Provenance** (:mod:`asas_llm.provenance`): which prompt version, model and
  trace made each stored output, collected with :func:`recording`.
* **Per-call knobs** (:mod:`asas_llm.overrides`): the model a call runs on,
  its output cap and the approved models, by call name, without a deploy.
* **Masking** (:mod:`asas_llm.masking`): untrusted text kept out of the trace
  store by key prefix.
* **Evals** (:mod:`asas_llm.evals`): one prompt replayed over recorded cases,
  checked and judged, with a pass rate.

Optional extras, each imported only when used: ``[langfuse]`` for the registry
and tracer, ``[langchain]`` for the runner, ``[openai]`` for the two model
factories, ``[guard]`` for the Azure scanner. ``import asas_llm`` alone needs
pydantic and json-repair.

Extracted from an AI engine's working implementation (the runner, the strict
schema walker, the Langfuse prompt builder, the LangChain-1.x shim, and the
gateway layers above).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .context import (
    RequestScope,
    active_trace_id,
    bind_request,
    clear_request,
    current_scope,
    current_session_id,
    last_trace_id,
    new_id,
    record_trace,
    request_scope,
    trace_ids,
)
from .admission import Admission, current_lane, lane
from .content_safety import UNTRUSTED, AzureContentSafety, ContentScanner, NoScanner, as_texts, untrusted
from .errors import (
    EmptyOutput,
    GatewayBusy,
    GenerationFailed,
    LLMError,
    ModelNotAllowed,
    NotConfigured,
    PromptFormatError,
    PromptUnavailable,
    StructuredOutputError,
    UnsafeContent,
    busy_cause,
    refusal_cause,
)
from .guard import GUARD_EVENTS, GuardPolicy, guarded
from .masking import REDACTED, mask_prefixed
from .overrides import ModelAllowlist, ModelOverrides, OutputCaps
from .prompts import (
    InMemoryPromptStore,
    LayeredPromptStore,
    LocalPromptStore,
    Prompt,
    PromptStore,
)
from .provenance import CallProvenance, OutputProvenance, PromptRef, recording
from .slots import WideSemaphore
from .structured import parse_structured, response_format, strict_json_schema, strip_code_fence
from .tracing import NullTracer, TraceHandle, Tracer, traced

if TYPE_CHECKING:  # pragma: no cover
    from .runners import LLMRunner, ModelFactory

__version__ = "0.1.0"

__all__ = [
    "GUARD_EVENTS",
    "REDACTED",
    "UNTRUSTED",
    "Admission",
    "AzureContentSafety",
    "CallProvenance",
    "ContentScanner",
    "EmptyOutput",
    "GatewayBusy",
    "GenerationFailed",
    "GuardPolicy",
    "InMemoryPromptStore",
    "LLMError",
    "LayeredPromptStore",
    "LocalPromptStore",
    "ModelAllowlist",
    "ModelNotAllowed",
    "ModelOverrides",
    "NoScanner",
    "NotConfigured",
    "NullTracer",
    "OutputCaps",
    "OutputProvenance",
    "Prompt",
    "PromptFormatError",
    "PromptRef",
    "PromptStore",
    "PromptUnavailable",
    "RequestScope",
    "StructuredOutputError",
    "TraceHandle",
    "Tracer",
    "UnsafeContent",
    "WideSemaphore",
    "active_trace_id",
    "as_texts",
    "bind_request",
    "busy_cause",
    "clear_request",
    "configure",
    "current_lane",
    "current_scope",
    "current_session_id",
    "guarded",
    "lane",
    "last_trace_id",
    "mask_prefixed",
    "new_id",
    "parse_structured",
    "record_trace",
    "recording",
    "refusal_cause",
    "request_scope",
    "response_format",
    "runner",
    "set_runner",
    "strict_json_schema",
    "strip_code_fence",
    "trace_ids",
    "traced",
    "untrusted",
    "__version__",
]

_settings: dict[str, Any] | None = None
_runner: "LLMRunner | None" = None


def configure(
    *,
    prompts: PromptStore,
    model_factory: "ModelFactory",
    default_model: str,
    tracer: Tracer | None = None,
    default_label: str | None = None,
    flush_after_trace: bool = False,
    overrides: ModelOverrides | None = None,
    output_caps: OutputCaps | None = None,
    allowlist: ModelAllowlist | None = None,
    refusal_kind: type[BaseException] | None = None,
    busy_kind: Any = None,
) -> None:
    """Install the host's pieces; :func:`runner` builds the ``LLMRunner`` from
    them on first use. Reconfiguring drops any built runner. The library reads
    no settings: the host builds the factory from its own configuration.

    ``overrides``, ``output_caps`` and ``allowlist`` are the per-call knobs
    (:mod:`asas_llm.overrides`); the guard and admission ride the model
    factory (:func:`asas_llm.guard.guarded` builds the client class the
    factory makes)."""
    global _settings, _runner
    _settings = {
        "prompts": prompts,
        "model_factory": model_factory,
        "default_model": default_model,
        "tracer": tracer,
        "default_label": default_label,
        "flush_after_trace": flush_after_trace,
        "overrides": overrides,
        "output_caps": output_caps,
        "allowlist": allowlist,
        "refusal_kind": refusal_kind,
        "busy_kind": busy_kind,
    }
    _runner = None


def runner() -> "LLMRunner":
    """The process-wide runner (lazy; see :func:`set_runner` for tests)."""
    global _runner
    if _runner is None:
        if _settings is None:
            raise NotConfigured(
                "asas_llm is not configured: call asas_llm.configure(...) at startup, "
                "or set_runner(instance) directly"
            )
        from .runners import LLMRunner

        _runner = LLMRunner(**_settings)
    return _runner


def set_runner(instance: "LLMRunner | None") -> None:
    """Override the runner (tests point this at one with a fake model);
    ``None`` resets to lazy re-selection from :func:`configure`."""
    global _runner
    _runner = instance
