"""The model calls, on an asas-agent release: the numbered lines in, a typed answer out.

Every call runs the ``profile-builder`` agent once (:func:`asas_agent.run_agent`): the
release's prompt, the input as the one message, no skills, no tools, the answer
validated into the call's pydantic type. The release is the agent as configuration
(``agent.yaml``: reasoning effort, tracing), the model and the provider are the host's,
built with asas-agent's :func:`~asas_agent.gateway_provider` or
:func:`~asas_agent.azure_provider`, whose client retries throttled and failed calls.

The CV text comes first in each message and what to fill last, so the provider's prompt
cache holds the shared part across one CV's calls.

:class:`Asker` holds what one event loop shares: a cap on calls at once across a CV,
and, when the host gives the key's tokens a minute, a leaky bucket (aiolimiter) that
waits until the minute has room, the prompt counted with tiktoken.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, create_model

from .model import Group
from .seams import Ask, CallResult

log = logging.getLogger(__name__)

#: the agent this package ships: ``agents/profile-builder/{agent.yaml,prompt.md}``
AGENTS = Path(__file__).resolve().parent / "agents"
AGENT = "profile-builder"
#: what an answer may cost on top of the prompt: schema, thinking and output
ANSWER_ROOM = 8_000


#: the reasoning efforts OpenAI's reasoning models take (which ones a model accepts varies by model)
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh")


def default_release(model: str, agents: Path = AGENTS, effort: str | None = None) -> dict:
    """The profile builder's release for ``model``, from the files under ``agents``
    (the package's own by default). ``effort`` replaces the folder's reasoning effort, so
    one prompt serves a model that wants another (a small model at ``none``); the id is
    the hash of the release as it will run. A host that keeps releases in a store reads
    one with :func:`asas_agent.fetch_release` instead, one release per effort."""
    from asas_agent import build_release, release_id

    release = build_release(agents, model, agent=AGENT)
    if effort is None:
        return release
    if effort not in EFFORTS:
        raise ValueError(f"effort is one of {', '.join(EFFORTS)}, not {effort!r}")
    settings = dict(release.get("settings") or {})
    settings["model"] = {**(settings.get("model") or {}), "reasoning": {**((settings.get("model") or {}).get("reasoning") or {}), "effort": effort}}
    release = {k: v for k, v in release.items() if k != "id"} | {"settings": settings}
    return release | {"id": release_id(release)}


class _Plain:
    """A one-shot agent's context renderer: no skills, no tools, no record."""

    always: frozenset[str] = frozenset()
    ask_tool = ""
    activate_skill = "Not available for this agent."
    no_skill = "This agent has no skills: answer from the instructions and the input."

    def head(self, snapshot):
        return []

    def tail(self, snapshot):
        return []

    def record(self, result):
        return None

    def opening(self, record):
        return []


async def _no_tools(*_args) -> dict:
    return {"ok": False, "code": "no_tools", "message": "This agent has no tools and asks nothing."}


@lru_cache(maxsize=1)
def _encoding() -> Any:
    """The model's tokenizer, or None where its vocabulary cannot be had: tiktoken fetches
    it once and keeps it (``TIKTOKEN_CACHE_DIR``), so a host with no way out seeds that
    folder when it builds its image."""
    import tiktoken

    try:
        return tiktoken.get_encoding("o200k_base")
    except Exception:  # noqa: BLE001 no network and no seeded cache
        log.warning("tiktoken's o200k_base is not available; tokens are estimated from the text's length")
        return None


def _tokens(text: str) -> int:
    enc = _encoding()
    return len(enc.encode(text)) if enc else len(text) // 2 + 1  # two characters a token over-counts English and Arabic alike


class Asker:
    """The calls of a run, on one release and one provider (or a model the host wraps,
    such as :class:`asas_agent.GuardedModel`). ``parallel`` caps calls at once;
    ``tokens_per_minute`` keeps them under the key's limit (0: no limit). Bound to the
    event loop of its first call, as the provider's client is.

    A failed call is recorded, not raised, as its error's type (and HTTP status), never
    its message: a failed answer check repeats the answer, so the message can hold CV
    text, and the record is logged and stored. ``raises`` names the errors the host
    wants raised instead, such as its guard's refusal."""

    def __init__(self, release: Mapping[str, Any], *, provider: Any = None, run_model: Any = None, parallel: int = 3,
                 tokens_per_minute: int = 0, requests_per_minute: int = 0,
                 raises: tuple[type[BaseException], ...] = ()):
        if (provider is None) == (run_model is None):
            raise ValueError("Asker needs a provider or a run_model, not both")
        self.release = {**release, "skills": {}}
        self.provider, self.run_model = provider, run_model
        self.parallel, self.tokens_per_minute, self.requests_per_minute = parallel, tokens_per_minute, requests_per_minute
        self.raises = raises
        self._loop: asyncio.AbstractEventLoop | None = None
        self._gate: asyncio.Semaphore | None = None
        self._limiter: Any = None
        self._requests: Any = None
        self._fixed: dict[Any, int] = {}  # tokens of the instructions and of each output type's schema, counted once

    @property
    def model(self) -> str:
        return self.release["model"]

    def need(self, text: str, output_type: Any = None) -> int:
        """The tokens a call counts against the key's minute: the whole request (the release's
        instructions, the output schema and the message) plus room for the answer. OpenAI's
        rate-limit guide counts the request's input, not the message alone; its parallel
        processor counts each request before sending it (openai-cookbook api_request_parallel_processor.py)."""
        if "instructions" not in self._fixed:
            self._fixed["instructions"] = _tokens(str(self.release.get("instructions") or ""))
        if output_type is not None and output_type not in self._fixed:
            self._fixed[output_type] = _tokens(json.dumps(output_type.model_json_schema()))
        return self._fixed["instructions"] + self._fixed.get(output_type, 0) + _tokens(text) + ANSWER_ROOM

    async def _spend(self, text: str, output_type: Any = None) -> None:
        """Wait until the key's minute has room for this call, in requests and in tokens (two
        leaky buckets, as OpenAI's parallel processor keeps); a tenth kept back for the estimate."""
        if not (self.requests_per_minute or self.tokens_per_minute):
            return
        from aiolimiter import AsyncLimiter

        if self.requests_per_minute:
            if self._requests is None:
                self._requests = AsyncLimiter(max(1, self.requests_per_minute * 0.9), 60)
            await self._requests.acquire(1)
        if not self.tokens_per_minute:
            return
        if self._limiter is None:
            self._limiter = AsyncLimiter(self.tokens_per_minute * 0.9, 60)
        await self._limiter.acquire(min(self.need(text, output_type), self.tokens_per_minute * 0.9))

    async def __call__(self, name: str, text: str, output_type: type[BaseModel], what: str) -> CallResult:
        from asas_agent import run_agent
        from openai import APIStatusError

        loop = asyncio.get_running_loop()
        if self._loop is not loop:  # asyncio's gate belongs to the loop it first waits in: a host's second loop gets its own
            self._loop, self._gate, self._limiter, self._requests = loop, asyncio.Semaphore(self.parallel), None, None
        snapshot = {"release": self.release, "tools": [], "history": [{"role": "user", "content": text}]}
        out = CallResult(name)
        start = time.perf_counter()
        log.info("%s: sent (%s)", name, what)
        try:
            async with self._gate:  # type: ignore[union-attr]
                await self._spend(text, output_type)
                result = await run_agent(snapshot, _no_tools, _no_tools, renderer=_Plain(), provider=self.provider,
                                         run_model=self.run_model, output_type=output_type)
            out.output, out.usage = result["output"], {k: v for k, v in result["usage"].items() if k != "calls"}
        except Exception as exc:  # noqa: BLE001 a schema refusal, a timeout or a bad answer: recorded, not raised
            if self.raises and isinstance(exc, self.raises):
                raise
            out.error = f"{exc.status_code} {type(exc).__name__}" if isinstance(exc, APIStatusError) else type(exc).__name__
        out.seconds = round(time.perf_counter() - start, 1)
        log.info("%s: %s in %ss%s", name, f"failed: {out.error}" if out.error else "done", out.seconds,
                 f", {out.usage.get('input_tokens', 0)} in / {out.usage.get('output_tokens', 0)} out" if out.usage else "")
        return out


# ---------------------------------------------------------------- the messages
def head(numbered: str) -> str:
    last = numbered.rsplit("\n", 1)[-1].split(" ", 1)[0] if numbered else "L000"
    return f"CV, one numbered line each, {last} the last line:\n\n{numbered}\n\n---\n"


def fill_message(numbered: str, group: Group) -> str:
    sections = "; ".join(f"{s.group} > {s.name}" if s.group else s.name for s in group.subs)
    return head(numbered) + f"Fill these sections only: {sections}."


async def fill(ask: Ask, numbered: str, group: Group) -> CallResult:
    return await ask(group.name, fill_message(numbered, group), group.model, f"{group.properties} properties")


class LineFix(BaseModel):
    ref: str = Field(description="The ref as given in the list.")
    lines: list[list[int]] = Field(description="The ranges [first, last] of the lines it was taken from; empty when the CV does not hold it.")


class LineFixes(BaseModel):
    fixes: list[LineFix]


async def fix_lines(ask: Ask, numbered: str, problems: list[dict]) -> CallResult:
    """One short call for the parts of an answer that cited lines the text does not have
    (Resume-Matcher retries a parse its validator rejects; here only the bad parts are
    asked again, so a long CV is not read twice for one slip)."""
    asked = "\n".join(f"- {p['ref']}: {p['what']}, {p['preview']}; cited {p['cited']}" for p in problems)
    msg = head(numbered) + ("Your answer cited lines that are not in the CV, or none, for these parts. Give each one's ranges again, "
                            f"from the lines above only:\n{asked}")
    return await ask("LineFix", msg, LineFixes, f"{len(problems)} parts")


#: what a block of other information is, when the host's ``kind`` field lists no values
OTHER_KINDS = ("Achievements", "References", "Availability", "Personal details", "Interests", "Declaration", "Summary",
               "Research and grants", "Teaching", "Training", "Skills", "Other")


def other_kinds_model(kinds: tuple[str, ...]) -> type[BaseModel]:
    kind_type: Any = Literal[kinds]  # the host's kinds, a type built at run time
    block: Any = create_model(
        "OtherKind",
        block=(int, Field(description="The block's number as given.")),
        kind=(kind_type, Field(description="What the block is.")),
        repeats=(list[int], Field(description="Lines outside the block that already say everything the block says, in any "
                                              "language: a translation, or the same text twice. Empty when the block adds anything.")),
    )
    return create_model("OtherKinds", blocks=(list[block], ...))


def spans_label(nums: list[int], width: int) -> str:
    """Line numbers as the CV's own labels, runs joined: "L005-L007, L009"."""
    runs: list[list[int]] = []
    for n in nums:
        if runs and n == runs[-1][1] + 1:
            runs[-1][1] = n
        else:
            runs.append([n, n])
    return ", ".join(f"L{a:0{width}d}" if a == b else f"L{a:0{width}d}-L{b:0{width}d}" for a, b in runs)


async def kind_other(ask: Ask, blocks: list[tuple[str, list[int]]], numbered: str, kinds: tuple[str, ...] = OTHER_KINDS) -> CallResult:
    """One short call over the whole CV that names what each block of other information
    is, and which other lines, if any, already say what it says (a bilingual CV states
    most things twice, and the fields cite one copy)."""
    width = max(3, len(numbered.rsplit("\n", 1)[-1].split(" ", 1)[0]) - 1)  # as the CV's own line numbers are written
    listed = "\n".join(f"Block {i}: {spans_label(nums, width)}, under the heading \"{topic}\"" for i, (topic, nums) in enumerate(blocks, 1))
    msg = (head(numbered) + "These blocks are parts of the CV above that no field of the profile took. Give each block one kind "
           "from the list, by what its lines say, not by its heading alone. In repeats, give the lines elsewhere in the CV that "
           "already say everything the block says, in any language; leave it empty when the block says anything they do not.\n\n" + listed)
    return await ask("OtherKinds", msg, other_kinds_model(kinds), f"{len(blocks)} blocks")
