"""The guard: every chat completion inspected before it is sent and before
its answer is used, in the one place that sees them all.

Two layers:

* :class:`GuardPolicy` decides. It runs the built-in rules
  (:mod:`asas_llm.rules`) and the host's :class:`~asas_llm.content_safety.ContentScanner`
  on the prompt (only its untrusted text, when the runner said which) and on
  the answer, and settles each finding per direction by its mode: ``monitor``
  (the default) logs ``ai_guard.finding`` with the call's name and the rules
  that matched, never the text, and lets the call through; ``enforce``
  refuses it; ``off`` skips the check. Every finding is counted in
  :data:`GUARD_EVENTS` and handed to ``on_finding`` for a metric.
* :func:`guarded` builds a chat client class with the policy built in.
  LangChain sends every chat completion through exactly one of
  ``_agenerate``, ``_astream``, ``_generate`` or ``_stream``, and a
  structured-output or tool-bound model is the same client underneath, so
  overriding those four is the one place that sees every request and every
  answer, whichever agent made it. The same seam holds the admission slot
  (:mod:`asas_llm.admission`) around the request alone, times the request,
  and adds the outbound headers the host wants on every call.

    policy = GuardPolicy(prompt_mode="enforce", scanner=AzureContentSafety(...))
    GuardedChatOpenAI = guarded(ChatOpenAI, policy=policy, admission=admission.slot)
    llm = GuardedChatOpenAI(model="gpt-5.1", name="parse-cv", api_key=..., base_url=...)

A mode may be a string or a zero-argument callable returning one, so a host
whose settings are live is read on every call.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from time import perf_counter
from typing import Any

from . import content_safety, rules
from .errors import UnsafeContent

log = logging.getLogger(__name__)

#: (direction, rule) -> times seen in this process; for tests and a metric.
GUARD_EVENTS: Counter[tuple[str, str]] = Counter()

MODES = frozenset({"off", "monitor", "enforce"})

Mode = str | Callable[[], str]


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # multimodal parts
        return "\n".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
    return "" if content is None else str(content)


def message_texts(messages: list[Any]) -> list[str]:
    """Every message's text, plus the arguments of any tool call it carries."""
    out = []
    for m in messages:
        out.append(_text(getattr(m, "content", m)))
        for call in getattr(m, "tool_calls", None) or []:
            out.append(json.dumps(call.get("args", {}), ensure_ascii=False, default=str))
    return out


def system_prompt(messages: list[Any]) -> str | None:
    """The first system message's text, for the leak check."""
    for m in messages:
        if type(m).__name__ == "SystemMessage" or getattr(m, "type", None) == "system":
            return _text(m.content)
    return None


def result_text(result: Any) -> str:
    """The text of a LangChain ``ChatResult``: every generation's content,
    tool-call arguments and parsed structured output."""
    parts = []
    for gen in getattr(result, "generations", None) or []:
        msg = getattr(gen, "message", None)
        parts.append(_text(msg.content) if msg is not None else getattr(gen, "text", ""))
        for call in getattr(msg, "tool_calls", None) or []:
            parts.append(json.dumps(call.get("args", {}), ensure_ascii=False, default=str))
        extra = getattr(msg, "additional_kwargs", None) or {}
        if extra.get("parsed") is not None:
            parsed = extra["parsed"]
            parts.append(parsed.model_dump_json() if hasattr(parsed, "model_dump_json") else str(parsed))
    return "\n".join(p for p in parts if p)


class GuardPolicy:
    """What to check and what to do about a finding. See the module docstring."""

    def __init__(
        self,
        *,
        prompt_mode: Mode = "monitor",
        response_mode: Mode = "monitor",
        scanner: content_safety.ContentScanner | None = None,
        refusal: Callable[[str, str | None, list[str]], Exception] = UnsafeContent,
        on_finding: Callable[[str, str | None, list[str], str], None] | None = None,
    ) -> None:
        self._prompt_mode = prompt_mode
        self._response_mode = response_mode
        self.scanner: content_safety.ContentScanner = scanner or content_safety.NoScanner()
        self._refusal = refusal
        self._on_finding = on_finding

    def mode(self, direction: str) -> str:
        raw = self._prompt_mode if direction == "prompt" else self._response_mode
        mode = str((raw() if callable(raw) else raw) or "monitor").strip().lower()
        return mode if mode in MODES else "monitor"

    def settle(self, direction: str, call: str | None, found: list[str]) -> None:
        """Log (and count) a finding; refuse when this direction enforces."""
        if not found:
            return
        mode = self.mode(direction)
        for rule in found:
            GUARD_EVENTS[(direction, rule)] += 1
        log.warning("ai_guard.finding direction=%s call=%s mode=%s rules=%s", direction, call, mode, ",".join(found))
        if self._on_finding is not None:
            try:
                self._on_finding(direction, call, found, mode)
            except Exception:  # noqa: BLE001 - a metric must never fail the call
                log.debug("ai_guard.on_finding failed", exc_info=True)
        if mode == "enforce":
            raise self._refusal(direction, call, found)

    async def inspect_prompt(self, messages: list[Any], call: str | None) -> None:
        if self.mode("prompt") == "off":
            return
        texts = message_texts(messages)
        # The scanner sees the data the call carries (what an attacker can
        # control), as the runner named it; the host's own prompt text stays here.
        untrusted = content_safety.UNTRUSTED.get()
        found = rules.prompt_findings(texts) + await self.scanner.prompt_findings(
            texts if untrusted is None else untrusted
        )
        self.settle("prompt", call, sorted(set(found)))

    async def inspect_response(self, text: str, messages: list[Any], call: str | None) -> None:
        if self.mode("response") == "off":
            return
        found = rules.response_findings(text, system_prompt(messages))
        found += await self.scanner.response_findings(text)
        self.settle("response", call, sorted(set(found)))

    def inspect_prompt_sync(self, messages: list[Any], call: str | None) -> None:
        """The rules alone: a sync call cannot await the scanner."""
        if self.mode("prompt") != "off":
            self.settle("prompt", call, rules.prompt_findings(message_texts(messages)))

    def inspect_response_sync(self, text: str, messages: list[Any], call: str | None) -> None:
        if self.mode("response") != "off":
            self.settle("response", call, rules.response_findings(text, system_prompt(messages)))


Admission = Callable[[str | None], Any]  # name -> async context manager
Timer = Callable[[str | None], Any]  # name -> context manager
Headers = Callable[[], dict[str, str]]


@asynccontextmanager
async def _nothing_async() -> AsyncIterator[None]:
    yield


@contextmanager
def _nothing() -> Iterator[None]:
    yield


def guarded(
    base: type,
    *,
    policy: GuardPolicy,
    admission: Admission | None = None,
    timer: Timer | None = None,
    outbound_headers: Headers | None = None,
    name: str | None = None,
) -> type:
    """A subclass of a LangChain chat model class (``ChatOpenAI``,
    ``AzureChatOpenAI``) with the guard, the admission slot, the timer and
    the outbound headers built into its four completion methods.

    ``admission(name)`` returns an async context manager held around the
    request alone, never the checks. ``timer(name)`` returns a context
    manager around the request (a host's histogram). ``outbound_headers()``
    returns headers added to every request (a correlation id), where a header
    the caller set itself wins.
    """

    def with_headers(kwargs: dict[str, Any]) -> dict[str, Any]:
        headers = outbound_headers() if outbound_headers else {}
        if not headers:
            return kwargs
        return {**kwargs, "extra_headers": {**headers, **(kwargs.get("extra_headers") or {})}}

    def slot(call: str | None):
        return admission(call) if admission else _nothing_async()

    def timed(call: str | None):
        return timer(call) if timer else _nothing()

    class _Guarded(base):  # type: ignore[misc,valid-type]
        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            call = getattr(self, "name", None)
            await policy.inspect_prompt(messages, call)
            async with slot(call):
                with timed(call):
                    result = await super()._agenerate(messages, stop=stop, run_manager=run_manager, **with_headers(kwargs))
            await policy.inspect_response(result_text(result), messages, call)
            return result

        async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
            call = getattr(self, "name", None)
            await policy.inspect_prompt(messages, call)
            seen: list[str] = []
            async with slot(call):
                with timed(call):
                    async for chunk in super()._astream(messages, stop=stop, run_manager=run_manager, **with_headers(kwargs)):
                        seen.append(chunk.text or "")
                        yield chunk
            # A stream is judged once it is whole: a rule can span chunks.
            await policy.inspect_response("".join(seen), messages, call)

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            call = getattr(self, "name", None)
            policy.inspect_prompt_sync(messages, call)
            with timed(call):
                result = super()._generate(messages, stop=stop, run_manager=run_manager, **with_headers(kwargs))
            policy.inspect_response_sync(result_text(result), messages, call)
            return result

        def _stream(self, messages, stop=None, run_manager=None, **kwargs):
            call = getattr(self, "name", None)
            policy.inspect_prompt_sync(messages, call)
            seen: list[str] = []
            with timed(call):
                for chunk in super()._stream(messages, stop=stop, run_manager=run_manager, **with_headers(kwargs)):
                    seen.append(chunk.text or "")
                    yield chunk
            policy.inspect_response_sync("".join(seen), messages, call)

    _Guarded.__name__ = name or f"Guarded{base.__name__}"
    _Guarded.__qualname__ = _Guarded.__name__
    _Guarded.__doc__ = f"``{base.__name__}`` with the guard (asas_llm.guard)."
    _Guarded.guard_policy = policy  # type: ignore[attr-defined]
    return _Guarded


@contextmanager
def measured(sink: Callable[[str | None, float, str], None], call: str | None) -> Iterator[None]:
    """A timer for :func:`guarded`: ``sink(call, seconds, outcome)`` after
    the request, outcome ``ok`` or ``error``."""
    started = perf_counter()
    outcome = "ok"
    try:
        yield
    except BaseException:
        outcome = "error"
        raise
    finally:
        sink(call, perf_counter() - started, outcome)


__all__ = [
    "GUARD_EVENTS",
    "MODES",
    "GuardPolicy",
    "guarded",
    "measured",
    "message_texts",
    "result_text",
    "system_prompt",
]
