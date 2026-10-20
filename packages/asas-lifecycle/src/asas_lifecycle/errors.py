"""Typed errors. Every one carries structured fields and a stable ``code``, so a
caller branches on data and a client reads the envelope, never the prose.

``message`` is English for logs and humans. Anything a program needs (the
states, the action, what would have been allowed) is an attribute and appears
in :meth:`LifecycleError.as_dict`, the one envelope shape for this package.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


class LifecycleError(Exception):
    """Base for everything this package raises on purpose."""

    code = "lifecycle_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def details(self) -> dict[str, Any]:
        return {}

    def as_dict(self) -> dict[str, Any]:
        """``{"code", "message", **details}``, JSON-safe (states as their string
        values). Hand it to your HTTP layer as the error body."""
        return {"code": self.code, "message": self.message, **self.details()}


@dataclass(frozen=True)
class Problem:
    """One thing wrong with a lifecycle definition."""

    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


class DefinitionError(LifecycleError, ValueError):
    """The definition is wrong. Raised at construction, so at import or boot,
    listing **every** problem at once rather than the first."""

    code = "lifecycle_definition_invalid"

    def __init__(self, lifecycle: str, problems: list[Problem]) -> None:
        self.lifecycle = lifecycle
        self.problems = tuple(problems)
        lines = "\n".join(f"  - [{p.code}] {p.message}" for p in self.problems)
        super().__init__(f"lifecycle {lifecycle!r} is invalid:\n{lines}")

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(p.code for p in self.problems)

    def details(self) -> dict[str, Any]:
        return {
            "lifecycle": self.lifecycle,
            "problems": [p.as_dict() for p in self.problems],
        }


class UnknownStateError(LifecycleError, ValueError):
    """A value that is not one of the lifecycle's states was passed at runtime
    (a typo, a stale row, a state from another enum)."""

    code = "lifecycle_unknown_state"

    def __init__(self, lifecycle: str, value: Any, known: tuple[str, ...]) -> None:
        self.lifecycle = lifecycle
        self.value = value
        self.known = known
        super().__init__(
            f"{value!r} is not a state of lifecycle {lifecycle!r} "
            f"(known: {', '.join(known)})"
        )

    def details(self) -> dict[str, Any]:
        return {"lifecycle": self.lifecycle, "value": str(self.value), "known": list(self.known)}


class InvalidTransition(LifecycleError):
    """A move the lifecycle refuses. ``reason`` says which kind of refusal:

    - ``not_allowed``: no edge goes from ``from_state`` to ``to_state``;
    - ``from_terminal``: ``from_state`` is terminal, so nothing leaves it;
    - ``unknown_action``: no edge with that action leaves ``from_state``
      (or the named action does not lead to ``to_state``);
    - ``not_initial``: :meth:`Lifecycle.start` with a state records do not
      start in.

    ``allowed`` is the states reachable from ``from_state`` in one step,
    ignoring guards, so a client can offer the real choices.
    """

    code = "invalid_transition"

    def __init__(
        self,
        *,
        lifecycle: str,
        from_state: Optional[str],
        to_state: Optional[str],
        action: Optional[str],
        allowed: tuple[str, ...],
        reason: str,
        message: str,
    ) -> None:
        self.lifecycle = lifecycle
        self.from_state = from_state
        self.to_state = to_state
        self.action = action
        self.allowed = allowed
        self.reason = reason
        super().__init__(message)

    def details(self) -> dict[str, Any]:
        return {
            "lifecycle": self.lifecycle,
            "from": _value(self.from_state),
            "to": _value(self.to_state),
            "action": self.action,
            "allowed": [_value(s) for s in self.allowed],
            "reason": self.reason,
        }


class GuardRefused(InvalidTransition):
    """The edge exists, but a host guard said no for this context. ``guard`` is
    the guard's name and ``guard_description`` its sentence."""

    code = "transition_guard_refused"

    def __init__(self, *, guard: str, guard_description: str, **kwargs: Any) -> None:
        self.guard = guard
        self.guard_description = guard_description
        super().__init__(reason="guard_refused", **kwargs)

    def details(self) -> dict[str, Any]:
        return {
            **super().details(),
            "guard": self.guard,
            "guard_description": self.guard_description,
        }


def _value(state: Any) -> Optional[str]:
    if state is None:
        return None
    return str(getattr(state, "value", state))


def to_http(error: LifecycleError, status_code: int = 409):
    """A FastAPI ``HTTPException`` carrying :meth:`LifecycleError.as_dict` as its
    ``detail``. 409 by default: a refused transition is a conflict with the
    record's current state. FastAPI is imported here only, so the package has no
    hard dependency on it."""
    from fastapi import HTTPException

    return HTTPException(status_code=status_code, detail=error.as_dict())
