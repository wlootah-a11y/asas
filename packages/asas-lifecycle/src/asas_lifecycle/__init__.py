"""Asas lifecycle: a small typed state machine for record statuses.

The host declares, next to its own status ``StrEnum``, which moves are allowed
(optionally named, optionally guarded by a host callable), which states a
record starts in and which are final. The definition is validated when it is
built, so a typo'd state, an unreachable state or a terminal state with a way
out fails at import, not in production. At runtime the service asks
``transition(current, target)`` and gets either a record of the move or a typed
error carrying the from/to/action and the states that *would* have been
allowed.

Extracted from a production system that carried five near-identical copies
of a hand-rolled status table. This is the thing *below*
``asas-workflow``: one record's status and its legal moves, with no approvals,
quorums, instances or tables.

Public surface: the Asas host contract, table-less **and** router-less
variant (no session dependency, no ``seed``/``migrate``/``build_routers``):

- :class:`Lifecycle`: the machine. ``transition``, ``fire`` (by action),
  ``start`` (the creation event), ``can_transition``, ``allowed_from``,
  ``edges_from``, ``available`` (guards applied), ``is_terminal``,
  ``describe`` (sentences) and ``catalog`` (machine-readable).
- :class:`Edge`, :class:`Guard` and :func:`guard`: what a host declares.
- :class:`TransitionRecord`: what a move produced, returned and handed to the
  optional ``sink`` callable. History is the host's: the package owns no table.
- :func:`catalog`, :func:`lifecycles`, :func:`get`: every lifecycle in the
  process (construction registers; nothing to call). :func:`reset` for tests.
- Errors: :class:`InvalidTransition`, :class:`GuardRefused`,
  :class:`UnknownStateError`, :class:`DefinitionError` (all
  :class:`LifecycleError`, each with ``as_dict()``), and :func:`to_http` for a
  FastAPI 409 when FastAPI is installed.
"""

from .errors import (
    DefinitionError,
    GuardRefused,
    InvalidTransition,
    LifecycleError,
    Problem,
    UnknownStateError,
    to_http,
)
from .machine import (
    Edge,
    Guard,
    Lifecycle,
    Sink,
    Transition,
    TransitionRecord,
    catalog,
    get,
    guard,
    lifecycles,
    reset,
)

__version__ = "0.1.0"

__all__ = [
    "DefinitionError",
    "Edge",
    "Guard",
    "GuardRefused",
    "InvalidTransition",
    "Lifecycle",
    "LifecycleError",
    "Problem",
    "Sink",
    "Transition",
    "TransitionRecord",
    "UnknownStateError",
    "catalog",
    "get",
    "guard",
    "lifecycles",
    "reset",
    "to_http",
    "__version__",
]
