"""Typed failures, each carrying the trace id of the call that failed.

The engine this package was extracted from swallowed every exception inside the
runner and returned ``None``. Every caller then had to remember to check for
``None``, most did, some did not, and the one thing an operator needed to find
the failure afterwards (the trace id) was never in the log line or the error.
Here a failure is an exception, it names the trace it belongs to, and a caller
that wants "try the next area" writes ``except LLMError`` and says so.
"""

from __future__ import annotations

from typing import Any


class LLMError(Exception):
    """Base of every failure this package raises.

    ``trace_id`` is the id of the trace the failing call ran under (``None``
    when it failed before a trace existed, e.g. the prompt could not be
    loaded). ``prompt`` is the prompt name when one is known.
    """

    def __init__(
        self,
        message: str,
        *,
        trace_id: str | None = None,
        prompt: str | None = None,
    ) -> None:
        super().__init__(message)
        self.trace_id = trace_id
        self.prompt = prompt

    def __str__(self) -> str:  # pragma: no cover - formatting only
        base = super().__str__()
        bits = []
        if self.prompt:
            bits.append(f"prompt={self.prompt!r}")
        if self.trace_id:
            bits.append(f"trace_id={self.trace_id}")
        return f"{base} ({', '.join(bits)})" if bits else base


class NotConfigured(LLMError):
    """``asas_llm.runner()`` was called before ``configure()``."""


class PromptUnavailable(LLMError):
    """No source could serve the prompt: the registry was unreachable or has no
    such prompt, the last-good copy is empty, and the local fallback has no
    file for it. The message names every source that was tried."""


class PromptFormatError(LLMError):
    """A local prompt file exists but cannot be read as a prompt."""


class GenerationFailed(LLMError):
    """The model provider raised. The original exception is ``__cause__``."""


class EmptyOutput(LLMError):
    """The model returned nothing usable (no text at all)."""


class UnsafeContent(LLMError):
    """The guard refused a prompt before it was sent, or a response before it
    was used. ``rules`` names what matched, never the text itself."""

    def __init__(
        self,
        direction: str,
        call: str | None,
        rules: list[str],
        *,
        trace_id: str | None = None,
    ) -> None:
        self.direction = direction
        self.call = call
        self.rules = list(rules)
        super().__init__(
            f"AI guard refused the {direction} of {call or 'a model call'}: {', '.join(self.rules)}",
            trace_id=trace_id,
            prompt=call,
        )


class ModelNotAllowed(LLMError):
    """A request named a model that is not on the approved list."""

    def __init__(self, model: str, purpose: str = "chat") -> None:
        self.model = model
        self.purpose = purpose
        super().__init__(f"Model {model!r} is not on the approved list ({purpose})")


class GatewayBusy(LLMError):
    """The model gateway refused or timed out past the client's own retries:
    a rate limit, or the gateway (or the proxy in front of it) overloaded.
    Not "no answer": a caller should ask again later, and no layer between
    here and the caller should ask again now. ``cause`` is the SDK error."""

    def __init__(
        self,
        call: str | None,
        cause: BaseException | None = None,
        *,
        trace_id: str | None = None,
    ) -> None:
        self.call = call
        self.cause = cause
        reason = type(cause).__name__ if cause is not None else "busy"
        status = getattr(cause, "status_code", None)
        super().__init__(
            f"The model gateway is busy ({reason}{f' {status}' if status else ''}) "
            f"for {call or 'a model call'}; retry later",
            trace_id=trace_id,
            prompt=call,
        )


#: Statuses that mean "too much right now", not "this request is wrong":
#: a rate limit, and the gateway (or the proxy in front of it) overloaded.
BUSY_STATUSES = frozenset({429, 502, 503, 504})


def _causes(exc: BaseException | None):
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


def busy_cause(exc: BaseException | None) -> BaseException | None:
    """The error that makes ``exc`` a busy gateway, or ``None``.

    Follows ``__cause__`` / ``__context__``, because LangChain or LangGraph
    may re-raise the SDK's error wrapped in their own. Recognises the OpenAI
    SDK's rate-limit, connection and busy-status errors when that SDK is
    installed, and any error carrying a ``status_code`` in
    :data:`BUSY_STATUSES` otherwise.
    """
    try:
        import openai  # noqa: WPS433 - optional
    except ImportError:  # pragma: no cover - a host without the SDK
        openai = None  # type: ignore[assignment]
    for candidate in _causes(exc):
        if isinstance(candidate, GatewayBusy):
            return candidate
        if openai is not None:
            # APITimeoutError is an APIConnectionError: both mean the request
            # never got an answer, which retrying later may fix.
            if isinstance(candidate, (openai.RateLimitError, openai.APIConnectionError)):
                return candidate
            if isinstance(candidate, openai.APIStatusError) and candidate.status_code in BUSY_STATUSES:
                return candidate
        elif getattr(candidate, "status_code", None) in BUSY_STATUSES:
            return candidate
    return None


def refusal_cause(exc: BaseException | None, kind: type[BaseException] = UnsafeContent) -> BaseException | None:
    """The guard's refusal behind ``exc``, or ``None``: a refusal is the same
    on every ask, so no layer retries it and a runner never reads it as "no
    answer". ``kind`` lets a host look for its own refusal class."""
    for candidate in _causes(exc):
        if isinstance(candidate, kind):
            return candidate
    return None


class StructuredOutputError(LLMError):
    """The model's text could not be turned into the requested model, even after
    repair. ``raw`` holds the (truncated) text so the failure can be inspected
    without re-running the call; ``errors`` is pydantic's error list when
    validation was what failed."""

    RAW_LIMIT = 4000

    def __init__(
        self,
        message: str,
        *,
        raw: str,
        errors: Any = None,
        trace_id: str | None = None,
        prompt: str | None = None,
    ) -> None:
        super().__init__(message, trace_id=trace_id, prompt=prompt)
        self.raw = raw[: self.RAW_LIMIT]
        self.errors = errors
