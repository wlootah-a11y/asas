"""Which prompt, model and trace produced a stored AI output.

A stored score (or a parsed profile, or an assessment) that carries no record
of how it was made cannot be audited or told stale: the prompt can be edited
in the registry, the model swapped by an override, and the row looks the same
as before. Two things are kept here so it can:

* :class:`PromptRef`: the prompt as the runner resolved it. The registry's
  name and version (and the label it was pinned to, if any), or, for a copy
  served from the cache or a local file, that source with the version it was
  snapshotted at and a digest of the text. A version is named only when the
  registry named it: the audit trail must not say a fallback was version N
  when nobody can prove it.
* :class:`CallProvenance`: one model call: that prompt, the model it actually
  ran on (after overrides), the reasoning effort asked for and the trace it
  was recorded under.

The runner records a ``CallProvenance`` for every call that answered, into the
collector opened by :func:`recording`. A caller wraps the calls whose output it
keeps and gets their records back, without any agent changing what it returns
(their outputs are the models' structured-output schemas, which a provenance
field would put in front of the model).

    with provenance.recording() as calls:
        parsed = await runner.ainvoke("parse-cv", {...}, output_model=ParsedCV)
    stored = {"result": parsed.value.model_dump(),
              "provenance": provenance.of_calls(calls, identity_value).model_dump()}

:func:`identity` is what, besides the input, decides a call's output: the
prompt (by registry version, or by text digest for a local copy) and the
model. A stored output keeps the digest of its identity, and a later read that
finds a different identity knows the output is stale without re-reading its
inputs.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from pydantic import BaseModel

from .prompts import Prompt

#: Key in a LangChain template's ``metadata`` under which a resolved
#: :class:`PromptRef` rides (as a dict), so the runner finds it on any template.
PROMPT_REF_KEY = "ai_prompt_ref"

#: Sources whose ``version`` is the registry's own and names the prompt.
REGISTRY_SOURCES = frozenset({"langfuse", "registry"})


class PromptRef(BaseModel):
    """A prompt as the runner resolved it. ``version`` is the registry's and
    is ``None`` for a copy the registry did not serve (``source`` is then
    ``cache``, ``local``, ``memory`` or ``repo``): a fallback is not any
    version, and the audit trail must not say it was."""

    name: str
    source: str
    version: int | None = None
    label: str | None = None
    #: sha256 of the prompt text, first 16 hex chars.
    sha: str | None = None

    @property
    def from_registry(self) -> bool:
        return self.source in REGISTRY_SOURCES


class CallProvenance(BaseModel):
    """One model call that answered."""

    call: str | None = None
    model: str
    prompt_name: str | None = None
    prompt_source: str | None = None
    prompt_version: int | None = None
    prompt_label: str | None = None
    prompt_sha: str | None = None
    #: The reasoning effort asked for; ``None`` is the deployment's default.
    effort: str | None = None
    trace_id: str | None = None


class OutputProvenance(BaseModel):
    """How a stored output was made: every call that went into it, and the
    digest of the prompt/model identity it was made under (the part of the
    output's fingerprint that is not its input), so a stored output can be
    told stale without re-reading its inputs."""

    identity: str | None = None
    calls: list[CallProvenance] = []


def text_digest(template: Any) -> str:
    """A short, stable digest of any JSON-able value (a prompt's text, an
    identity dict)."""
    blob = json.dumps(template, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def ref_of(prompt: Prompt, *, label: str | None = None) -> PromptRef:
    """The :class:`PromptRef` of a :class:`~asas_llm.prompts.Prompt` as a
    store served it. Only a registry-served prompt names its version."""
    served_by_registry = prompt.source in REGISTRY_SOURCES
    return PromptRef(
        name=prompt.name,
        source=prompt.source,
        version=prompt.version if served_by_registry else None,
        label=label,
        sha=text_digest(prompt.content),
    )


def langfuse_ref(name: str, prompt_client: Any, label: str | None) -> PromptRef:
    """A ref for a raw Langfuse prompt client (a host that still builds its
    templates by hand)."""
    body = (
        prompt_client.get_langchain_prompt()
        if hasattr(prompt_client, "get_langchain_prompt")
        else getattr(prompt_client, "prompt", None)
    )
    return PromptRef(
        name=name,
        source="langfuse",
        version=getattr(prompt_client, "version", None),
        label=label,
        sha=text_digest(body),
    )


def repo_ref(name: str, template: Any) -> PromptRef:
    """A ref for a copy held in the host's own repository: no version, on
    purpose."""
    return PromptRef(name=name, source="repo", sha=text_digest(template))


def prompt_ref(template: Any) -> PromptRef | None:
    """The ref attached to a LangChain template under :data:`PROMPT_REF_KEY`,
    if any (a template built by hand has none)."""
    metadata = getattr(template, "metadata", None) or {}
    ref = metadata.get(PROMPT_REF_KEY)
    if isinstance(ref, PromptRef):
        return ref
    if isinstance(ref, dict):
        return PromptRef.model_validate(ref)
    return None


def identity(ref: PromptRef | None, model: str, effort: str | None = None) -> dict:
    """What, besides the input, decides a call's output: the prompt and the
    model. For a cache key / fingerprint. A registry prompt is named by its
    version (a new version is a new prompt, even if the text came back the
    same); any other copy has none, so its text digest stands in."""
    if ref is None:
        prompt: dict = {"name": None}
    elif ref.from_registry:
        prompt = {"name": ref.name, "version": ref.version}
    else:
        prompt = {"name": ref.name, "source": ref.source, "sha": ref.sha}
    return {"prompt": prompt, "model": model, "effort": effort}


def of_calls(calls: list[CallProvenance], identity_value: Any) -> OutputProvenance:
    """An output's provenance: the calls that made it, and the digest of the
    :func:`identity` it was made under (compared on the next read)."""
    return OutputProvenance(identity=text_digest(identity_value), calls=list(calls))


_SINK: ContextVar[list[CallProvenance] | None] = ContextVar("asas_llm_call_provenance", default=None)


@contextmanager
def recording() -> Iterator[list[CallProvenance]]:
    """Collect the provenance of every model call made inside the block.

    The list is shared by reference with any task started inside it (a task
    copies the context, not the list), so the calls a fan-out makes in its own
    tasks land here too. Nested blocks each see only their own calls.
    """
    calls: list[CallProvenance] = []
    token = _SINK.set(calls)
    try:
        yield calls
    finally:
        _SINK.reset(token)


def record(
    *,
    ref: PromptRef | None = None,
    prompt: Any = None,
    model: str,
    effort: str | None = None,
    trace_id: str | None = None,
    call: str | None = None,
) -> None:
    """Record one answered call into the open :func:`recording`, if any.
    ``ref`` is the resolved prompt; ``prompt`` is accepted instead for a
    LangChain template carrying :data:`PROMPT_REF_KEY`."""
    sink = _SINK.get()
    if sink is None:
        return
    if ref is None and prompt is not None:
        ref = prompt_ref(prompt)
    sink.append(
        CallProvenance(
            call=call,
            model=model,
            prompt_name=ref.name if ref else None,
            prompt_source=ref.source if ref else None,
            prompt_version=ref.version if ref else None,
            prompt_label=ref.label if ref else None,
            prompt_sha=ref.sha if ref else None,
            effort=effort,
            trace_id=trace_id,
        )
    )


__all__ = [
    "PROMPT_REF_KEY",
    "REGISTRY_SOURCES",
    "CallProvenance",
    "OutputProvenance",
    "PromptRef",
    "identity",
    "langfuse_ref",
    "of_calls",
    "prompt_ref",
    "record",
    "recording",
    "ref_of",
    "repo_ref",
    "text_digest",
]
