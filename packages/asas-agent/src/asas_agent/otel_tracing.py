"""The SDK's traces in Langfuse over OpenTelemetry: a ``TracingProcessor`` that
writes every run, turn, model call and tool call through the Langfuse Python SDK
v3 and later (OpenTelemetry-based; built and tested on 5.1).

:class:`~asas_agent.tracing.LangfuseTracingProcessor` writes through the 2.x
client's ``trace``, ``generation`` and ``span`` calls. A Langfuse v4 server in
its events-only ingestion mode refuses those events (HTTP 207, "Event type not
accepted", which the 2.x client drops without a word), so on such a server a
host needs this processor instead. The mapping is the same, so a host chooses
one processor and nothing ``run_agent`` sends changes:

* the SDK trace becomes a Langfuse trace with the same id (the SDK's 32 hex,
  less its ``trace_`` prefix); its root observation is named by the workflow
  name, which is also the trace name, and the group id is the session;
* the trace's user, tags and version come from the reserved metadata keys of
  :mod:`asas_agent.trace_attributes` (stripped from the metadata forwarded); the
  release, which Langfuse v3+ sets per client and not per trace, is forwarded as
  the metadata key ``asas_release``; the rest of the metadata is the trace's
  metadata, every value a string (Langfuse drops a value over 200 characters);
* every observation of the trace carries those attributes: they are set through
  ``langfuse.propagate_attributes`` in a context of the trace's own, which the
  processor makes current only while it opens one of the trace's observations,
  so no span of the host's (a web request's, say) receives them;
* a ``generation`` or ``response`` span becomes a Langfuse generation with the
  model, the input, the output and the token usage; inside ``run_agent`` it is
  linked to the Langfuse prompt version the release was read from, and the run's
  model names a generation whose span names none (the Responses API's);
* the root observation, which Langfuse shows as the trace itself, gets the run's
  input and answer when the trace ends: the first model call's input and the last
  model call's output, as those generations sent them (masked); and, when any of
  its observations ended with an error, the ``ERROR`` level and the first error's
  message, so the trace reads as failed in Langfuse's lists;
* a generation's usage is in Langfuse's buckets (``usage_buckets``): ``input``,
  ``output``, ``total``, and the cached input and reasoning output tokens apart
  from them, so a cached token costs the model's cached price;
* an ``agent``, ``function`` or ``guardrail`` span becomes a Langfuse agent,
  tool or guardrail observation, every other span a Langfuse span, named by its
  kind and name, with its data as input; a tool call keeps its arguments and
  result.

An observation opens when its SDK span STARTS and closes when it ENDS, so both
timestamps are the real ones and the nesting is the SDK's. OpenTelemetry exports
a span only once it ends, so a call that never finishes leaves nothing
half-written. An error on a span is the observation's ``ERROR`` level and status
message. ``mask`` works as in the 2.x processor: applied to every input, output
and data payload; a mask that raises sends ``"[mask failed]"``, never the value.
The client's own ``mask`` and ``mask_otel_spans``, if the host sets them, apply
on top at export.

    from langfuse import Langfuse
    from agents import set_trace_processors
    from asas_agent import LangfuseOtelTracingProcessor

    client = Langfuse(public_key=..., secret_key=..., base_url=..., environment="dev")
    set_trace_processors([LangfuseOtelTracingProcessor(client, mask=mask)])

As with the 2.x processor, use ``set_trace_processors`` (which REPLACES the SDK's
default exporter to api.openai.com), never ``add_trace_processor``.

Imports the Agents SDK, so ``import asas_agent`` loads this module only when one
of its names is first used; Langfuse and OpenTelemetry are imported when a trace
starts, so the name resolves on a host with the 2.x extra too. Needs the
``langfuse-otel`` extra to trace.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
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
from .tracing import (
    GENERATION_KINDS,
    MASK_FAILED,
    Mask,
    PromptLink,
    flat_parameters,
    langfuse_id,
    response_io,
)

log = logging.getLogger(__name__)

#: The metadata key the release travels under (v3+ sets the release per client).
RELEASE_METADATA_KEY = "asas_release"

#: Langfuse drops a propagated value longer than this, with a warning.
MAX_VALUE = 200

#: The Langfuse observation type an SDK span kind becomes; any other is a span.
OBSERVATION_TYPES = {
    **{kind: "generation" for kind in GENERATION_KINDS},
    "agent": "agent",
    "function": "tool",
    "guardrail": "guardrail",
}


@dataclass
class _OpenTrace:
    context: Any  # the OpenTelemetry context the trace's attributes live in
    root: Any  # the root observation
    # The run's input and answer, masked, for the root (and so the trace) to show:
    # the first model call's input and the last model call's output.
    input: Any = None
    output: Any = None
    # The first error any observation of the trace ended with: the root carries
    # it too, so the trace itself reads ERROR in Langfuse's lists and filters.
    error: str | None = None


def _count(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _detail(details: Any, key: str) -> int:
    """A count from the SDK's ``*_tokens_details``: a dict, or a one-item list of
    one (``serialize_usage``'s shape)."""
    if isinstance(details, list) and details:
        details = details[0]
    return (_count(details.get(key)) or 0) if isinstance(details, dict) else 0


def usage_buckets(usage: dict[str, Any]) -> dict[str, int] | None:
    """The SDK's usage as Langfuse's usage details: ``input``, ``output``, ``total``,
    and the cached and reasoning tokens as buckets of their own,
    ``input_cached_tokens`` and ``output_reasoning_tokens``, taken OUT of
    ``input`` and ``output`` (Langfuse adds every bucket's cost, so a bucket left
    inside its parent is counted twice). Langfuse prices a cached input token at
    its model's cached price (a tenth of the input price for the GPT-5 family),
    so without the bucket every cached token was charged as a full one."""
    given = _count(usage.get("input_tokens"))
    answered = _count(usage.get("output_tokens"))
    total = _count(usage.get("total_tokens"))
    cached = _detail(usage.get("input_tokens_details"), "cached_tokens")
    reasoning = _detail(usage.get("output_tokens_details"), "reasoning_tokens")
    buckets: dict[str, int] = {}
    if given is not None:
        buckets["input"] = max(given - cached, 0)
        if cached:
            buckets["input_cached_tokens"] = min(cached, given)
    if answered is not None:
        buckets["output"] = max(answered - reasoning, 0)
        if reasoning:
            buckets["output_reasoning_tokens"] = min(reasoning, answered)
    if total is not None:
        buckets["total"] = total
    return buckets or None


def _clip(value: Any) -> str:
    return str(value)[:MAX_VALUE]


def _attributes(trace: Trace) -> tuple[str, dict[str, Any], Any]:
    """The trace's name, its ``propagate_attributes`` keywords and its group id."""
    data = trace.export() or {}
    name = str(data.get("workflow_name") or trace.name or "trace")
    metadata = data.get("metadata")
    reserved: dict[str, Any] = {}
    forwarded: dict[str, str] = {}
    if isinstance(metadata, Mapping):
        for key, value in metadata.items():
            if key in RESERVED_KEYS:
                reserved[key] = value
            elif value is not None:
                forwarded[str(key)] = _clip(value)
    if reserved.get(RELEASE_KEY):
        forwarded[RELEASE_METADATA_KEY] = _clip(reserved[RELEASE_KEY])
    tags = [
        t.strip() for t in str(reserved.get(TAGS_KEY) or "").split(",") if t.strip()
    ]
    group = data.get("group_id")
    keywords: dict[str, Any] = {
        "trace_name": _clip(name),
        "user_id": _clip(reserved[USER_ID_KEY]) if reserved.get(USER_ID_KEY) else None,
        "session_id": _clip(group) if group else None,
        "version": _clip(reserved[VERSION_KEY]) if reserved.get(VERSION_KEY) else None,
        "tags": [_clip(t) for t in tags] or None,
        "metadata": forwarded or None,
    }
    return name, {k: v for k, v in keywords.items() if v is not None}, group


class LangfuseOtelTracingProcessor(TracingProcessor):
    """See the module docstring. ``client`` is a ``langfuse.Langfuse`` (v3 or
    later); ``flush_on_trace_end`` flushes after every run, for a short-lived
    worker."""

    def __init__(
        self, client: Any, *, mask: Mask | None = None, flush_on_trace_end: bool = False
    ) -> None:
        self.client = client
        self.mask = mask
        self.flush_on_trace_end = flush_on_trace_end
        self._shut_down = False
        self._lock = threading.Lock()
        self._traces: dict[str, _OpenTrace] = {}
        self._spans: dict[str, Any] = {}

    def _masked(self, value: Any) -> Any:
        if value is None or self.mask is None:
            return value
        try:
            return self.mask(data=value)
        except Exception as exc:  # noqa: BLE001 - a mask must never cost the trace
            # The exception's message and traceback may quote the value: type only.
            log.warning(
                "asas_agent.otel_tracing: mask failed (%s); field sent as %s",
                type(exc).__name__,
                MASK_FAILED,
            )
            return MASK_FAILED

    @contextmanager
    def _in(self, context: Any) -> Iterator[None]:
        from opentelemetry import context as otel_context

        token = otel_context.attach(context)
        try:
            yield
        finally:
            otel_context.detach(token)

    # -- the SDK's hooks -----------------------------------------------------

    def on_trace_start(self, trace: Trace) -> None:
        trace_id = langfuse_id(trace.trace_id)
        if not trace_id:
            return
        try:
            # Imported here, not at the top: every name the package exports resolves
            # whichever Langfuse extra a host installed (the 2.x one has neither).
            from langfuse import propagate_attributes
            from opentelemetry import context as otel_context

            name, keywords, _ = _attributes(trace)
            # An empty context: the attributes must not land on a span of the host's.
            with self._in(otel_context.Context()), propagate_attributes(**keywords):
                context = otel_context.get_current()
            with self._in(context):
                root = self.client.start_observation(
                    trace_context={"trace_id": trace_id}, name=name, as_type="span"
                )
            with self._lock:
                self._traces[trace_id] = _OpenTrace(context=context, root=root)
        except Exception:  # noqa: BLE001 - tracing must never fail the run
            log.warning(
                "asas_agent.otel_tracing: could not open trace %s",
                trace.trace_id,
                exc_info=True,
            )

    def on_trace_end(self, trace: Trace) -> None:
        with self._lock:
            opened = self._traces.pop(langfuse_id(trace.trace_id) or "", None)
        if opened is not None:
            try:
                # Langfuse shows the root observation's input and output as the
                # trace's own: without these the trace reads null / undefined.
                io = {
                    k: v
                    for k, v in (("input", opened.input), ("output", opened.output))
                    if v is not None
                }
                if opened.error is not None:
                    io["level"] = "ERROR"
                    io["status_message"] = opened.error
                if io:
                    opened.root.update(**io)
            except Exception:  # noqa: BLE001 - tracing must never fail the run
                log.warning(
                    "asas_agent.otel_tracing: could not write trace %s input/output",
                    trace.trace_id,
                    exc_info=True,
                )
            try:
                opened.root.end()
            except Exception:  # noqa: BLE001 - tracing must never fail the run
                log.warning(
                    "asas_agent.otel_tracing: could not close trace %s",
                    trace.trace_id,
                    exc_info=True,
                )
        if self.flush_on_trace_end:
            self.force_flush()

    def on_span_start(self, span: Span[Any]) -> None:
        try:
            with self._lock:
                opened = self._traces.get(langfuse_id(span.trace_id) or "")
                parent = (
                    self._spans.get(span.parent_id or "") if span.parent_id else None
                )
            if opened is None:
                return  # the trace was opened before this processor, or failed to open
            kind = str(getattr(span.span_data, "type", "span") or "span")
            as_type = OBSERVATION_TYPES.get(kind, "span")
            with self._in(opened.context):
                observation = (parent or opened.root).start_observation(
                    name=kind, as_type=as_type
                )
            with self._lock:
                self._spans[span.span_id] = observation
        except Exception:  # noqa: BLE001 - tracing must never fail the run
            log.warning(
                "asas_agent.otel_tracing: could not open span %s",
                span.span_id,
                exc_info=True,
            )

    def on_span_end(self, span: Span[Any]) -> None:
        with self._lock:
            observation = self._spans.pop(span.span_id, None)
        if observation is None:
            return
        try:
            data = span.export() or {}
            span_data = dict(data.get("span_data") or {})
            kind = str(span_data.pop("type", "span"))
            fields = (
                self._generation(kind, span_data, span.span_data)
                if kind in GENERATION_KINDS
                else self._span(kind, span_data)
            )
            if kind in GENERATION_KINDS:
                self._remember_io(span.trace_id, fields, span_data)
            error = data.get("error")
            if error:
                fields["level"] = "ERROR"
                fields["status_message"] = str(
                    error.get("message") if isinstance(error, dict) else error
                )
                self._remember_error(span.trace_id, fields["status_message"])
            observation.update(**fields)
        except Exception:  # noqa: BLE001 - tracing must never fail the run
            log.warning(
                "asas_agent.otel_tracing: could not write span %s",
                span.span_id,
                exc_info=True,
            )
        finally:
            try:
                observation.end()
            except Exception:  # noqa: BLE001
                log.debug("asas_agent.otel_tracing: could not end span", exc_info=True)

    def _remember_error(self, trace_id: str | None, message: str) -> None:
        with self._lock:
            opened = self._traces.get(langfuse_id(trace_id) or "")
            if opened is not None and opened.error is None:
                opened.error = message

    def _remember_io(
        self, trace_id: str | None, fields: dict[str, Any], span_data: dict[str, Any]
    ) -> None:
        """Keep a model call's (already masked) input and output for its trace's
        root: the first input the run sent, the last answer it got. An output that
        is only the response id (sensitive data off) is no answer and is skipped."""
        given, answer = fields.get("input"), fields.get("output")
        if answer is not None and answer == {"response_id": span_data.get("response_id")}:
            answer = None
        with self._lock:
            opened = self._traces.get(langfuse_id(trace_id) or "")
            if opened is None:
                return
            if opened.input is None and given is not None:
                opened.input = given
            if answer is not None:
                opened.output = answer

    def _generation(
        self, kind: str, span_data: dict[str, Any], raw: Any = None
    ) -> dict[str, Any]:
        link = RUN_LINK.get()
        # The Responses API span exports no model: the response object has it, and
        # failing that the run knows the name it asked for.
        response_model = getattr(getattr(raw, "response", None), "model", None)
        model = (
            span_data.get("model")
            or (response_model if isinstance(response_model, str) else None)
            or (link.model if link else None)
        )
        usage_details = usage_buckets(span_data.get("usage") or {})
        given, answered = response_io(raw) if kind == "response" else (None, None)
        output = span_data.get("output")
        if output is None:
            output = answered
        if output is None and span_data.get("response_id"):
            output = {"response_id": span_data["response_id"]}
        fields: dict[str, Any] = {
            "name": span_data.get("name") or kind,
            "model": model,
            "model_parameters": flat_parameters(span_data.get("model_config")),
            "input": self._masked(span_data.get("input") or given),
            "output": self._masked(output),
            "usage_details": usage_details,
            "metadata": {"kind": kind},
        }
        if link and link.prompt_name and link.prompt_version is not None:
            fields["prompt"] = PromptLink(link.prompt_name, link.prompt_version)
        return fields

    def _span(self, kind: str, span_data: dict[str, Any]) -> dict[str, Any]:
        name = span_data.get("name") or kind
        payload = {
            k: v
            for k, v in span_data.items()
            if k not in ("name", "input", "output") and v is not None
        }
        metadata: dict[str, Any] = {"kind": kind}
        if "input" in span_data and payload:
            metadata["data"] = self._masked(payload)
        return {
            "name": f"{kind}:{name}" if name != kind else kind,
            "input": self._masked(
                span_data.get("input") if "input" in span_data else (payload or None)
            ),
            "output": self._masked(span_data.get("output")),
            "metadata": metadata,
        }

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
            log.debug("asas_agent.otel_tracing: flush failed", exc_info=True)


__all__ = [
    "OBSERVATION_TYPES",
    "RELEASE_METADATA_KEY",
    "LangfuseOtelTracingProcessor",
]
