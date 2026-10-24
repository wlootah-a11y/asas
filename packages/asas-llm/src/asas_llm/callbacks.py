"""LangChain callbacks the runner attaches to a call. Needs the ``[langchain]``
extra; not imported by ``import asas_llm``."""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult

log = logging.getLogger(__name__)


def finish_reason(generation: Any) -> str | None:
    info = getattr(generation, "generation_info", None) or {}
    if info.get("finish_reason"):
        return info["finish_reason"]
    message = getattr(generation, "message", None)
    metadata = getattr(message, "response_metadata", None) or {}
    return metadata.get("finish_reason")


class TruncatedOutputLogger(BaseCallbackHandler):
    """Logs a generation that stopped at its token cap, as its own error.

    A call that stops at its cap (``finish_reason == "length"``) has a cut
    answer. It was indistinguishable from a model that answered badly: the
    JSON was repaired or failed to parse, and the log said "parse failed" at
    best. Logged as ``llm_output_truncated`` with the call's name and cap."""

    def __init__(self, name: str | None, max_tokens: int | None) -> None:
        self.call = name
        self.max_tokens = max_tokens

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        for generations in response.generations:
            for generation in generations:
                if finish_reason(generation) == "length":
                    log.error(
                        "llm_output_truncated name=%s max_tokens=%s: the model stopped at its output cap, the answer is cut short",
                        self.call,
                        self.max_tokens,
                    )
                    return


__all__ = ["TruncatedOutputLogger", "finish_reason"]
