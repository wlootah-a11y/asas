"""What a run tells Langfuse about itself, without the Agents SDK.

:class:`TraceAttributes` is the host's side: who the run is for, how to find it
(tags, version, release) and a little metadata. :func:`reserved_metadata` is how
those travel: as reserved keys in the SDK trace's metadata, which
:class:`~asas_agent.tracing.LangfuseTracingProcessor` strips and maps to Langfuse's
own trace fields. A host that opens its own ``agents.tracing.trace(...)`` (to own the
trace id and name) writes the same keys itself; the contract is in the README.

:data:`RUN_LINK` is the other side: what ``run_agent`` knows about the run that the
SDK's spans do not carry (the model's name, the Langfuse prompt version that served
the release). It is a context variable read by the processor as each span ends, so it
reaches the generations whether the run opened the SDK trace or joined the host's.

Imports nothing from the SDK or Langfuse: safe in a workflow sandbox.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

#: Reserved keys in an SDK trace's metadata. The processor removes them from the
#: metadata it forwards and sends each as the Langfuse trace field it names.
USER_ID_KEY = "langfuse.user_id"
TAGS_KEY = "langfuse.tags"  # comma-joined; a tag holds no comma
VERSION_KEY = "langfuse.version"
RELEASE_KEY = "langfuse.release"
RESERVED_KEYS = frozenset({USER_ID_KEY, TAGS_KEY, VERSION_KEY, RELEASE_KEY})


@dataclass(frozen=True)
class TraceAttributes:
    """What Langfuse should know about one run's trace; every field is optional.

    ``session_id`` is the Langfuse session (``run_agent``'s ``group_id`` wins over
    it); ``tags`` are filterable labels (none may contain a comma); ``version`` and
    ``release`` are the host's own strings (for Langfuse's version and release
    filters); ``metadata`` is free-form, and ``run_agent``'s ``trace_metadata`` wins
    over it key by key."""

    user_id: str | None = None
    session_id: str | None = None
    tags: tuple[str, ...] = ()
    version: str | None = None
    release: str | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.tags, str):
            raise TypeError("TraceAttributes.tags is a tuple of strings, not a string")
        object.__setattr__(self, "tags", tuple(self.tags))
        for tag in self.tags:
            if "," in tag:
                raise ValueError(f"A trace tag holds no comma: {tag!r}")


def reserved_metadata(attributes: TraceAttributes | None) -> dict[str, str]:
    """The reserved metadata keys that carry `attributes` (only what is set)."""
    if attributes is None:
        return {}
    carried = {
        USER_ID_KEY: attributes.user_id,
        TAGS_KEY: ",".join(attributes.tags) or None,
        VERSION_KEY: attributes.version,
        RELEASE_KEY: attributes.release,
    }
    return {key: value for key, value in carried.items() if value}


@dataclass(frozen=True)
class RunLink:
    """What the run knows and its spans do not: the model's name, and the Langfuse
    prompt (name and version) that served the release, when one did."""

    model: str | None = None
    prompt_name: str | None = None
    prompt_version: int | None = None


#: Set by ``run_agent`` for the length of a run; read by the tracing processor.
RUN_LINK: ContextVar[RunLink | None] = ContextVar("asas_agent_run_link", default=None)


def prompt_of(release: Mapping[str, Any]) -> tuple[str, int] | None:
    """The Langfuse prompt (name, version) a release was read from, or None for a
    release built from files or read from a store that has no such link."""
    source = release.get("source")
    if not isinstance(source, Mapping):
        return None
    name, version = source.get("name"), source.get("version")
    if (
        isinstance(name, str)
        and isinstance(version, int)
        and not isinstance(version, bool)
    ):
        return name, version
    return None


__all__ = [
    "RELEASE_KEY",
    "RESERVED_KEYS",
    "RUN_LINK",
    "TAGS_KEY",
    "USER_ID_KEY",
    "VERSION_KEY",
    "RunLink",
    "TraceAttributes",
    "prompt_of",
    "reserved_metadata",
]
