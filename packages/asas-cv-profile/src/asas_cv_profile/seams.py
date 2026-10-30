"""The seams a host fills (ports, in the ports-and-adapters sense): protocols only,
importing nothing heavy, as asas-agent's own seams.

| Seam | What the host supplies | This package's adapter |
|---|---|---|
| :class:`Ask` | one model call: a name, the message, the answer's type | :class:`~asas_cv_profile.calls.Asker` on an asas-agent release |
| :class:`PdfReader` | a PDF's text as numbered lines | :class:`~asas_cv_profile.lines.LayoutReader` (Document Intelligence) |
| :class:`Finish` | the host's own computed fields | none: ``computed: host`` fields stay empty without one |
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:  # pragma: no cover
    from pydantic import BaseModel

    from .lines import Lines
    from .model import ProfileModel


@dataclass
class CallResult:
    """One call: its name, the typed answer or the error, seconds and tokens."""

    group: str
    output: Any = None
    error: str | None = None
    seconds: float = 0.0
    usage: dict = field(default_factory=dict)


class Ask(Protocol):
    """One model call: (call name, the message, the answer's pydantic type, what was
    asked, for the log) in, a :class:`CallResult` out. A failed call is a result with
    ``error`` set, never an exception: the run records it and the host reads the CV again
    later. ``release`` names the agent release the calls run on (its ``model`` and ``id``),
    recorded with every profile; a stand-in may leave it empty."""

    release: dict

    async def __call__(self, name: str, text: str, output_type: type[BaseModel], what: str, /) -> CallResult: ...


class PdfReader(Protocol):
    """A PDF's bytes in, its lines out; raises :class:`~asas_cv_profile.lines.Refused`
    with ``reader_unavailable`` when the service cannot be reached."""

    def read(self, data: bytes) -> Lines: ...


class Finish(Protocol):
    """The host's last step, after the package's own computed fields: (the profile, the
    profile model, the as-of date) in, warnings out. It fills the ``computed: host`` fields
    in place."""

    def __call__(self, profile: dict, model: ProfileModel, today: date, /) -> list[str]: ...
