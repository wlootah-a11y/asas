"""The SDK's traces in Langfuse: a ``TracingProcessor`` that writes every run,
turn, model call and tool call to the Langfuse 2.x client a host already has.

The Agents SDK traces a run as one trace with nested spans (agent, generation
or response, function, handoff, guardrail, custom). The SDK's own exporter
sends them to OpenAI; a product that keeps its audit trail in Langfuse wants
them beside the single-call traces its other code already writes there, in
the same session. This processor maps them one to one:

* the SDK trace becomes a Langfuse trace with the same id (less the SDK's
  ``trace_`` prefix, which leaves the 32 hex characters Langfuse wants), the
  workflow name as its name and the group id as its session;
* a ``generation`` or ``response`` span becomes a Langfuse generation, with
  the model, the input, the output and the token usage the SDK recorded;
* the trace's user, tags, version and release are read from reserved keys in the
  SDK trace's metadata (:mod:`asas_agent.trace_attributes`), removed from the
  metadata it forwards; ``run_agent`` writes them, and so can a host that opens its
  own trace;
* inside ``run_agent``, a generation of a release read from a Langfuse prompt is
  linked to that prompt version, and a generation whose span names no model (the
  Responses API's) gets the run's;
* every other span becomes a Langfuse span, named by its kind and name, with
  its data as input; a tool call keeps its arguments and result.

Spans are written when they END, with both timestamps, so a retry that never
finishes leaves nothing half-written. An error on a span is the observation's
``ERROR`` level and status message. A ``mask`` (the shape Langfuse's own
``MaskFunction`` has, ``mask(data=...)``) is applied to every input, output
and data payload before it is sent, so a transcript a run carries can be
recorded by reference rather than by value. If ``mask`` raises, that field
is sent as ``"[mask failed]"``, never as the raw value, and the failure is
logged without the content. With ``mask=None`` every payload is sent by value.

    from asas_agent.tracing import LangfuseTracingProcessor
    from agents import set_trace_processors
    set_trace_processors([LangfuseTracingProcessor(langfuse_client, mask=mask)])

``set_trace_processors`` REPLACES the SDK's processors, so the default OpenAI
exporter is gone. ``add_trace_processor`` would keep it, and that exporter
sends every trace to api.openai.com whenever an ``OPENAI_API_KEY`` is set: a
host that must keep data in-country must not use it. Do not turn the SDK's
exporter off with ``set_tracing_disabled(True)`` either, since that stops
every processor, this one included.

Imports the Agents SDK, so ``import asas_agent`` loads this module only when
one of its names is first used.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from agents.tracing import Span, Trace, TracingProcessor

from .trace_attributes import (
    RELEASE_KEY,
    RESERVED_KEYS,
    RUN_LINK,
    TAGS_KEY,
    USER_ID_KEY,
    VERSION_KEY,
)

log = logging.getLogger(__name__)

Mask = Callable[..., Any]

#: What a field is sent as when the mask raises on it: never the raw value.
MASK_FAILED = "[mask failed]"

#: Span kinds the SDK records a model call under.
GENERATION_KINDS = frozenset({"generation", "response"})


def langfuse_id(sdk_id: str | None) -> str | None:
    """A Langfuse id from an SDK one: ``trace_<hex>`` -> ``<hex>``; a span id
    is kept whole (``span_<hex>``), unique either way."""
    if sdk_id is None:
        return None
    return sdk_id[6:] if sdk_id.startswith("trace_") else sdk_id


def flat_parameters(config: Any) -> dict[str, Any] | None:
    """The model settings as Langfuse 2.x takes them: a flat map of scalars.
    The SDK records ``reasoning`` and the like as objects, which the client
    refuses; a nested value is sent as its JSON."""
    if not isinstance(config, Mapping):
        return None
    flat: dict[str, Any] = {}
    for key, value in config.items():
        if value is None:
            continue
        if isinstance(value, (str, int, float, bool)):
            flat[key] = value
        else:
            try:
                flat[key] = json.dumps(value, default=str, sort_keys=True)
            except (TypeError, ValueError):
                flat[key] = str(value)
    return flat or None


@dataclass(frozen=True)
class PromptLink:
    """What ``Langfuse.generation(prompt=...)`` reads of a prompt client: its name,
    its version and whether it is a fallback. The release records the first two, so
    the link needs no round trip to Langfuse."""

    name: str
    version: int
    is_fallback: bool = False


def _when(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class LangfuseTracingProcessor(TracingProcessor):
    """See the module docstring. ``client`` is a ``langfuse.Langfuse`` (2.x);
    ``flush_on_trace_end`` flushes after every run, for a short-lived worker."""

    def __init__(
        self, client: Any, *, mask: Mask | None = None, flush_on_trace_end: bool = False
    ) -> None:
        self.client = client
        self.mask = mask
        self.flush_on_trace_end = flush_on_trace_end
        self._shut_down = False

    def _masked(self, value: Any) -> Any:
        if value is None or self.mask is None:
            return value
        try:
            return self.mask(data=value)
        except Exception as exc:  # noqa: BLE001 - a mask must never cost the trace
            # The exception's message and traceback may quote the value, so
            # only its type is logged.
            log.warning(
                "asas_agent.tracing: mask failed (%s); field sent as %s",
                type(exc).__name__,
                MASK_FAILED,
            )
            return MASK_FAILED

    # -- the SDK's hooks -----------------------------------------------------

    def on_trace_start(self, trace: Trace) -> None:
        data = trace.export() or {}
        metadata = data.get("metadata")
        attributes: dict[str, Any] = {}
        if isinstance(metadata, Mapping):
            metadata = dict(metadata)
            reserved = {k: metadata.pop(k) for k in RESERVED_KEYS if k in metadata}
            tags = [
                t.strip()
                for t in str(reserved.get(TAGS_KEY) or "").split(",")
                if t.strip()
            ]
            attributes = {
                "user_id": reserved.get(USER_ID_KEY),
                "version": reserved.get(VERSION_KEY),
                "release": reserved.get(RELEASE_KEY),
                "tags": tags or None,
            }
            metadata = metadata or None
        try:
            self.client.trace(
                id=langfuse_id(trace.trace_id),
                name=data.get("workflow_name") or trace.name,
                session_id=data.get("group_id"),
                metadata=metadata,
                # Only what is set, so the client's own release stays when none is.
                **{k: str(v) if k != "tags" else v for k, v in attributes.items() if v},
            )
        except Exception:  # noqa: BLE001 - tracing must never fail the run
            log.warning(
                "asas_agent.tracing: could not open trace %s",
                trace.trace_id,
                exc_info=True,
            )

    def on_trace_end(self, trace: Trace) -> None:
        if self.flush_on_trace_end:
            self.force_flush()

    def on_span_start(self, span: Span[Any]) -> None:
        return None

    def on_span_end(self, span: Span[Any]) -> None:
        data = span.export() or {}
        span_data = dict(data.get("span_data") or {})
        kind = str(span_data.pop("type", "span"))
        error = data.get("error")
        common: dict[str, Any] = {
            "id": langfuse_id(span.span_id),
            "trace_id": langfuse_id(span.trace_id),
            "parent_observation_id": langfuse_id(span.parent_id),
            "start_time": _when(data.get("started_at")),
            "end_time": _when(data.get("ended_at")),
        }
        if error:
            common["level"] = "ERROR"
            common["status_message"] = str(
                error.get("message") if isinstance(error, dict) else error
            )
        try:
            if kind in GENERATION_KINDS:
                self._generation(kind, span_data, common, span.span_data)
            else:
                self._span(kind, span_data, common)
        except Exception:  # noqa: BLE001 - tracing must never fail the run
            log.warning(
                "asas_agent.tracing: could not write span %s",
                span.span_id,
                exc_info=True,
            )

    def _generation(
        self,
        kind: str,
        span_data: dict[str, Any],
        common: dict[str, Any],
        raw: Any = None,
    ) -> None:
        link = RUN_LINK.get()
        # The Responses API span exports no model: the response object has it, and
        # failing that the run knows the name it asked for.
        response_model = getattr(getattr(raw, "response", None), "model", None)
        model = (
            span_data.get("model")
            or (response_model if isinstance(response_model, str) else None)
            or (link.model if link else None)
        )
        prompt = (
            PromptLink(link.prompt_name, link.prompt_version)
            if link and link.prompt_name and link.prompt_version is not None
            else None
        )
        usage = span_data.get("usage") or {}
        usage_details = {
            k: int(v) for k, v in usage.items() if isinstance(v, (int, float))
        } or None
        output = span_data.get("output")
        if output is None and span_data.get("response_id"):
            output = {"response_id": span_data["response_id"]}
        self.client.generation(
            name=span_data.get("name") or kind,
            model=model,
            model_parameters=flat_parameters(span_data.get("model_config")),
            input=self._masked(span_data.get("input")),
            output=self._masked(output),
            usage_details=usage_details,
            metadata={"kind": kind},
            **({"prompt": prompt} if prompt else {}),
            **common,
        )

    def _span(
        self, kind: str, span_data: dict[str, Any], common: dict[str, Any]
    ) -> None:
        name = span_data.get("name") or kind
        payload = {
            k: v
            for k, v in span_data.items()
            if k not in ("name", "input", "output") and v is not None
        }
        self.client.span(
            name=f"{kind}:{name}" if name != kind else kind,
            input=self._masked(
                span_data.get("input") if "input" in span_data else (payload or None)
            ),
            output=self._masked(span_data.get("output")),
            metadata={
                "kind": kind,
                **(
                    {"data": self._masked(payload)}
                    if "input" in span_data and payload
                    else {}
                ),
            },
            **common,
        )

    def shutdown(self) -> None:
        """Flush what is pending, once; later calls do nothing and none raises. The
        client is the host's and stays open: other code writes through it."""
        if self._shut_down:
            return
        self._shut_down = True
        self.force_flush()

    def force_flush(self) -> None:
        try:
            self.client.flush()
        except Exception:  # noqa: BLE001 - tracing must never fail the run
            log.debug("asas_agent.tracing: flush failed", exc_info=True)


__all__ = [
    "GENERATION_KINDS",
    "MASK_FAILED",
    "LangfuseTracingProcessor",
    "PromptLink",
    "flat_parameters",
    "langfuse_id",
]
