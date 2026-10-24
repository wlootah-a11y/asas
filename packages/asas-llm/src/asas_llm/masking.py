"""Keep untrusted text out of the trace store, by key prefix.

A trace backend records every call's input and output. Two requirements pull
against each other: the model and prompt version recorded per call (an audit
needs them), and the text a person said NOT written to a store with its own
retention (a transcript is recorded by reference, not by value). Turning
tracing off satisfies the second and breaks the first. Langfuse takes a mask
instead, so the trace keeps model, prompt version, latency and token counts,
and carries a marker where the text would have been.

**The mask is registered once on the client, so it sees every caller's
payload.** Matching a bare key name like ``transcript`` therefore reaches
further than the one agent that asked for it. So the caller that needs masking
namespaces its keys (``screening_transcript``, ``screening_segment``) and the
mask matches the prefix, which keeps the blast radius inside that caller.

    from asas_llm.masking import mask_prefixed
    client = make_client(..., mask=mask_prefixed(("screening_",)))
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

REDACTED = "[redacted: untrusted text, recorded by reference]"

MaskFunction = Callable[..., Any]


def mask_prefixed(prefixes: Iterable[str], *, redacted: str = REDACTED) -> MaskFunction:
    """A mask that replaces the value of every key starting with one of
    ``prefixes``, anywhere in a traced payload (nested dicts and lists).

    The returned function takes keyword-only ``data``, the shape Langfuse's
    ``MaskFunction`` declares. It never raises: a mask that raises takes the
    trace down with it, and the trace is an audit requirement, so anything
    unrecognised passes through untouched.
    """
    prefixes = tuple(prefixes)

    def mask(*, data: Any) -> Any:
        try:
            if isinstance(data, dict):
                return {
                    key: (
                        redacted
                        if isinstance(key, str) and key.startswith(prefixes)
                        else mask(data=value)
                    )
                    for key, value in data.items()
                }
            if isinstance(data, (list, tuple)):
                return [mask(data=item) for item in data]
            return data
        except Exception:  # noqa: BLE001 - see the docstring
            return redacted

    mask.prefixes = prefixes  # type: ignore[attr-defined]
    return mask


__all__ = ["MaskFunction", "REDACTED", "mask_prefixed"]
