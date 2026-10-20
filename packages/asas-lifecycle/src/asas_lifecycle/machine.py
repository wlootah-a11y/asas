"""The lifecycle: a declared set of states, the edges between them, and the
checks that keep a record's status honest.

Pure: no database, no clock, no framework. A :class:`Lifecycle` is built once
(module level, next to the host's status enum), validated on construction, and
then asked questions at runtime.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Generic, Optional, TypeVar, Union

from .errors import (
    DefinitionError,
    GuardRefused,
    InvalidTransition,
    Problem,
    UnknownStateError,
)

S = TypeVar("S", bound=Enum)

#: A history sink: called with each :class:`TransitionRecord` after the move is
#: validated. The host persists it however it likes (its own table, its audit
#: log, its outbox). Whatever it raises propagates to the caller.
Sink = Callable[["TransitionRecord[Any]"], None]


# ── the pieces a host declares ──────────────────────────────────────────────


@dataclass(frozen=True)
class Guard:
    """A host-supplied condition on an edge. ``check(context)`` returns truthy to
    allow the move; ``context`` is whatever the caller passed to
    :meth:`Lifecycle.transition`. ``description`` completes the sentence
    "... only when <description>", so write it as a clause:
    ``"the job has never received an application"``."""

    name: str
    check: Callable[[Any], Any]
    description: str

    def __call__(self, context: Any) -> bool:
        return bool(self.check(context))


def guard(description: str, *, name: Optional[str] = None) -> Callable[[Callable[[Any], Any]], Guard]:
    """Decorator form of :class:`Guard`::

        @guard("the job has never received an application")
        def never_applied(ctx) -> bool:
            return ctx["application_count"] == 0
    """

    def wrap(fn: Callable[[Any], Any]) -> Guard:
        return Guard(name=name or fn.__name__, check=fn, description=description)

    return wrap


GuardLike = Union[Guard, Callable[[Any], Any]]


@dataclass(frozen=True)
class Edge:
    """One allowed move. ``from_state`` may be a single state or several (an
    edge from each); ``action`` optionally names the move (``"publish"``);
    ``guard`` is one :class:`Guard` or a sequence of them, all of which must
    pass. ``description`` is an optional note carried into the catalog.

    States may be given as enum members or their string values."""

    from_state: Any
    to_state: Any
    action: Optional[str] = None
    guard: Union[GuardLike, tuple[GuardLike, ...], list[GuardLike], None] = None
    description: str = ""


@dataclass(frozen=True)
class Transition(Generic[S]):
    """A validated, normalised edge: exactly one source state, guards resolved."""

    from_state: S
    to_state: S
    action: Optional[str]
    guards: tuple[Guard, ...] = ()
    description: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "from": self.from_state.value,
            "to": self.to_state.value,
            "action": self.action,
            "guards": [{"name": g.name, "description": g.description} for g in self.guards],
            "description": self.description,
        }


@dataclass(frozen=True)
class TransitionRecord(Generic[S]):
    """What happened, returned by :meth:`Lifecycle.transition` and handed to the
    sink. ``from_state`` is ``None`` for :meth:`Lifecycle.start` (the creation
    event). ``context`` is the caller's context, passed through untouched so a
    sink can reach the host's session, actor or tenant. No timestamp: the host
    stamps its own row, with its own clock."""

    lifecycle: str
    from_state: Optional[S]
    to_state: S
    action: Optional[str]
    context: Any = field(default=None, compare=False, repr=False)

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe, without ``context``."""
        return {
            "lifecycle": self.lifecycle,
            "from": None if self.from_state is None else self.from_state.value,
            "to": self.to_state.value,
            "action": self.action,
        }


# ── the lifecycle ───────────────────────────────────────────────────────────


class Lifecycle(Generic[S]):
    """A typed state machine over a host's status enum.

    ``name`` is the record's noun (``"job"``); it names the machine in errors,
    the catalog and the sentences. ``states`` is the host's ``StrEnum``.
    ``initial`` is the state (or states) a record may start in; ``terminal``
    the states nothing leaves. ``transitions`` is a list of :class:`Edge` or
    plain tuples ``(from, to)`` / ``(from, to, action)``. ``sink`` optionally
    receives every :class:`TransitionRecord`.

    Construction validates the whole definition and raises one
    :class:`DefinitionError` listing every problem.
    """

    def __init__(
        self,
        name: str,
        states: type[S],
        *,
        initial: Any,
        transitions: Iterable[Any],
        terminal: Any = (),
        sink: Optional[Sink] = None,
        description: str = "",
        register: bool = True,
    ) -> None:
        self.name = name
        self.description = description
        self.sink = sink
        problems: list[Problem] = []

        if not isinstance(name, str) or not name.strip():
            problems.append(Problem("invalid_name", "the lifecycle needs a non-empty name"))
        if not (isinstance(states, type) and issubclass(states, Enum)):
            raise DefinitionError(
                str(name),
                [Problem("states_not_enum", f"states must be an Enum class, got {states!r}")],
            )
        self._enum: type[S] = states
        members = tuple(states)
        if not members:
            problems.append(Problem("no_states", f"{states.__name__} has no members"))
        bad = [m.name for m in members if not isinstance(m.value, str)]
        if bad:
            problems.append(Problem(
                "state_value_not_str",
                f"every state value must be a string (use StrEnum); not: {', '.join(bad)}",
            ))
        self._states: tuple[S, ...] = members

        self._initial = frozenset(self._states_in(initial, "initial", problems))
        if not self._initial and not any(p.code == "unknown_state" for p in problems):
            problems.append(Problem("no_initial", "at least one initial state is required"))
        self._terminal = frozenset(self._states_in(terminal, "terminal", problems))

        self._edges: tuple[Transition[S], ...] = tuple(self._normalise(transitions, problems))
        self._check_graph(problems)

        if problems:
            raise DefinitionError(str(name), problems)

        self._out: dict[S, tuple[Transition[S], ...]] = {
            s: tuple(e for e in self._edges if e.from_state is s) for s in self._states
        }
        if register:
            _register(self)

    # ── definition-time helpers ─────────────────────────────────────────────

    def _coerce_def(self, value: Any, where: str, problems: list[Problem]) -> Optional[S]:
        try:
            return self._coerce(value)
        except UnknownStateError:
            problems.append(Problem(
                "unknown_state",
                f"{where} names {value!r}, which is not a {self._enum.__name__} state",
            ))
            return None

    def _states_in(self, value: Any, where: str, problems: list[Problem]) -> list[S]:
        out: list[S] = []
        for v in _as_many(value):
            s = self._coerce_def(v, where, problems)
            if s is not None and s not in out:
                out.append(s)
        return out

    def _normalise(self, transitions: Iterable[Any], problems: list[Problem]) -> list[Transition[S]]:
        out: list[Transition[S]] = []
        seen: set[tuple[S, S, Optional[str]]] = set()
        for i, raw in enumerate(transitions):
            if isinstance(raw, Edge):
                edge = raw
            elif isinstance(raw, tuple) and len(raw) in (2, 3):
                edge = Edge(*raw)
            else:
                problems.append(Problem(
                    "invalid_edge",
                    f"transition #{i} is {raw!r}; use Edge(...) or (from, to[, action])",
                ))
                continue
            where = f"transition #{i}"
            if edge.action is not None and (not isinstance(edge.action, str) or not edge.action.strip()):
                problems.append(Problem("invalid_action", f"{where} has an empty or non-string action"))
                continue
            guards = self._guards(edge.guard, where, problems)
            target = self._coerce_def(edge.to_state, f"{where} (to)", problems)
            sources = self._states_in(edge.from_state, f"{where} (from)", problems)
            if target is None or not sources:
                continue
            for src in sources:
                key = (src, target, edge.action)
                if key in seen:
                    problems.append(Problem(
                        "duplicate_edge",
                        f"{src.value} -> {target.value}"
                        + (f" by {edge.action}" if edge.action else "")
                        + " is declared twice",
                    ))
                    continue
                seen.add(key)
                out.append(Transition(src, target, edge.action, guards, edge.description))
        return out

    @staticmethod
    def _guards(value: Any, where: str, problems: list[Problem]) -> tuple[Guard, ...]:
        if value is None:
            return ()
        items = value if isinstance(value, (list, tuple)) else (value,)
        out: list[Guard] = []
        for g in items:
            if isinstance(g, Guard):
                if not g.description.strip():
                    problems.append(Problem("guard_undescribed", f"{where}: guard {g.name!r} has no description"))
                out.append(g)
            elif callable(g):
                name = getattr(g, "__name__", "")
                doc = (getattr(g, "__doc__", None) or "").strip().splitlines()
                if not name or name == "<lambda>" or not doc:
                    problems.append(Problem(
                        "guard_undescribed",
                        f"{where}: a guard needs a name and a sentence; wrap it in "
                        "Guard(name, check, description) or give the function a docstring",
                    ))
                    continue
                out.append(Guard(name=name, check=g, description=doc[0].rstrip(".")))
            else:
                problems.append(Problem("invalid_guard", f"{where}: {g!r} is not a Guard or callable"))
        return tuple(out)

    def _check_graph(self, problems: list[Problem]) -> None:
        outgoing: dict[S, list[Transition[S]]] = {s: [] for s in self._states}
        for e in self._edges:
            outgoing[e.from_state].append(e)

        for s in sorted(self._terminal, key=self._states.index):
            if outgoing[s]:
                targets = ", ".join(e.to_state.value for e in outgoing[s])
                problems.append(Problem(
                    "terminal_has_exits",
                    f"{s.value} is terminal but has outgoing transitions (to {targets})",
                ))
        for s in self._states:
            if s not in self._terminal and not outgoing[s]:
                problems.append(Problem(
                    "dead_end",
                    f"{s.value} has no outgoing transitions; declare it terminal "
                    "or give it a way out",
                ))

        by_action: dict[tuple[S, str], S] = {}
        for e in self._edges:
            if e.action is None:
                continue
            prior = by_action.setdefault((e.from_state, e.action), e.to_state)
            if prior is not e.to_state:
                problems.append(Problem(
                    "ambiguous_action",
                    f"action {e.action!r} from {e.from_state.value} leads to both "
                    f"{prior.value} and {e.to_state.value}",
                ))

        if self._initial:
            reached = set(self._initial)
            queue = deque(self._initial)
            while queue:
                for e in outgoing[queue.popleft()]:
                    if e.to_state not in reached:
                        reached.add(e.to_state)
                        queue.append(e.to_state)
            for s in self._states:
                if s not in reached:
                    problems.append(Problem(
                        "unreachable_state",
                        f"{s.value} cannot be reached from "
                        f"{_or(sorted((i.value for i in self._initial)))}",
                    ))

    # ── runtime ─────────────────────────────────────────────────────────────

    def _coerce(self, value: Any) -> S:
        if isinstance(value, self._enum):
            return value
        if isinstance(value, str) and not isinstance(value, Enum):
            try:
                return self._enum(value)
            except ValueError:
                pass
        raise UnknownStateError(
            str(self.name), value, tuple(str(m.value) for m in self._enum)
        )

    @property
    def states(self) -> tuple[S, ...]:
        return self._states

    @property
    def initial(self) -> frozenset[S]:
        return self._initial

    @property
    def terminal(self) -> frozenset[S]:
        return self._terminal

    @property
    def transitions(self) -> tuple[Transition[S], ...]:
        return self._edges

    def is_terminal(self, state: Any) -> bool:
        return self._coerce(state) in self._terminal

    def edges_from(self, state: Any) -> tuple[Transition[S], ...]:
        """Every edge leaving ``state``, in declaration order, guards ignored."""
        return self._out[self._coerce(state)]

    def allowed_from(self, state: Any) -> tuple[S, ...]:
        """The states one step away from ``state``, in declaration order,
        guards ignored. Empty for a terminal state."""
        out: list[S] = []
        for e in self.edges_from(state):
            if e.to_state not in out:
                out.append(e.to_state)
        return tuple(out)

    def available(self, state: Any, context: Any = None) -> tuple[Transition[S], ...]:
        """The edges leaving ``state`` whose guards pass for ``context``: what a
        UI or an agent can actually offer right now."""
        return tuple(e for e in self.edges_from(state) if all(g(context) for g in e.guards))

    def can_transition(
        self, from_state: Any, to_state: Any, *, action: Optional[str] = None, context: Any = None
    ) -> bool:
        """Whether :meth:`transition` would succeed, guards included, without
        recording anything. An unknown state still raises: a typo is a bug,
        not a "no"."""
        try:
            self._resolve(from_state, to_state, action, context)
        except InvalidTransition:
            return False
        return True

    def transition(
        self, from_state: Any, to_state: Any, *, action: Optional[str] = None, context: Any = None
    ) -> TransitionRecord[S]:
        """Validate ``from_state -> to_state`` (optionally by the named
        ``action``), run its guards against ``context``, hand the record to the
        sink, and return it. Raises :class:`InvalidTransition` or
        :class:`GuardRefused`; nothing is recorded on a refusal.

        Without ``action``, the first declared edge between the two states
        whose guards pass is taken, and the record carries its action."""
        edge = self._resolve(from_state, to_state, action, context)
        return self._record(edge.from_state, edge.to_state, edge.action, context)

    def fire(self, from_state: Any, action: str, *, context: Any = None) -> TransitionRecord[S]:
        """Apply the named ``action`` from ``from_state``; the lifecycle knows
        where it leads."""
        src = self._coerce(from_state)
        self._refuse_if_terminal(src, None, action)
        for e in self._out[src]:
            if e.action == action:
                return self.transition(src, e.to_state, action=action, context=context)
        raise self._refusal(
            src, None, action, "unknown_action",
            f"{_article(self.name)} {self.name} in {src.value} has no action {action!r}",
        )

    def start(self, state: Any = None, *, context: Any = None) -> TransitionRecord[S]:
        """The creation event: a record entering the lifecycle. ``state`` must
        be an initial state (it may be omitted when there is only one). The
        record's ``from_state`` is ``None``."""
        initial = tuple(s for s in self._states if s in self._initial)
        if state is None:
            if len(initial) != 1:
                raise InvalidTransition(
                    lifecycle=self.name, from_state=None, to_state=None, action=None,
                    allowed=initial, reason="not_initial",
                    message=f"{self.name} has several initial states; name one "
                    f"({_or([s.value for s in initial])})",
                )
            target = initial[0]
        else:
            target = self._coerce(state)
        if target not in self._initial:
            raise InvalidTransition(
                lifecycle=self.name, from_state=None, to_state=target, action=None,
                allowed=initial, reason="not_initial",
                message=f"{_article(self.name).capitalize()} {self.name} cannot start in "
                f"{target.value} (it starts in {_or([s.value for s in initial])})",
            )
        return self._record(None, target, None, context)

    # ── internals ───────────────────────────────────────────────────────────

    def _record(self, src: Optional[S], dst: S, action: Optional[str], context: Any) -> TransitionRecord[S]:
        record = TransitionRecord(self.name, src, dst, action, context)
        if self.sink is not None:
            self.sink(record)
        return record

    def _refusal(self, src: S, dst: Optional[S], action: Optional[str], reason: str, message: str) -> InvalidTransition:
        return InvalidTransition(
            lifecycle=self.name, from_state=src, to_state=dst, action=action,
            allowed=self.allowed_from(src), reason=reason, message=message,
        )

    def _refuse_if_terminal(self, src: S, dst: Optional[S], action: Optional[str]) -> None:
        if src in self._terminal:
            raise self._refusal(
                src, dst, action, "from_terminal",
                f"{_article(self.name).capitalize()} {self.name} in {src.value} is "
                "final and cannot move",
            )

    def _resolve(self, from_state: Any, to_state: Any, action: Optional[str], context: Any) -> Transition[S]:
        src, dst = self._coerce(from_state), self._coerce(to_state)
        self._refuse_if_terminal(src, dst, action)
        between = [e for e in self._out[src] if e.to_state is dst]
        candidates = between if action is None else [e for e in between if e.action == action]
        if not candidates:
            noun = f"{_article(self.name).capitalize()} {self.name}"
            if action is not None and between:
                named = sorted(e.action for e in between if e.action)
                raise self._refusal(
                    src, dst, action, "unknown_action",
                    f"{noun} moves from {src.value} to {dst.value} by "
                    f"{_or(named) if named else 'an unnamed transition'}, not {action!r}",
                )
            if action is not None and all(e.action != action for e in self._out[src]):
                raise self._refusal(
                    src, dst, action, "unknown_action",
                    f"{noun} in {src.value} has no action {action!r}",
                )
            raise self._refusal(
                src, dst, action, "not_allowed",
                f"{noun} cannot move from {src.value} to {dst.value}",
            )
        first_refusal: Optional[tuple[Transition[S], Guard]] = None
        for e in candidates:
            failed = next((g for g in e.guards if not g(context)), None)
            if failed is None:
                return e
            if first_refusal is None:
                first_refusal = (e, failed)
        assert first_refusal is not None
        e, g = first_refusal
        raise GuardRefused(
            guard=g.name, guard_description=g.description,
            lifecycle=self.name, from_state=src, to_state=dst, action=e.action,
            allowed=self.allowed_from(src),
            message=f"{_article(self.name).capitalize()} {self.name} can move from "
            f"{src.value} to {dst.value} only when {g.description}",
        )

    # ── explanation ─────────────────────────────────────────────────────────

    def describe(self) -> list[str]:
        """The whole lifecycle as sentences a product owner can check: where a
        record starts, every allowed move, and which states are final."""
        noun, art = _words(self.name), _article(self.name)
        starts = [s for s in self._states if s in self._initial]
        out = [f"{art.capitalize()} {noun} starts in {_or([_label(s) for s in starts])}."]
        out += [self._sentence(e) for e in self._edges]
        out += [
            f"{_label(s).capitalize()} is final: {art} {noun} never leaves it."
            for s in self._states if s in self._terminal
        ]
        return out

    def _sentence(self, e: Transition[S]) -> str:
        art, noun = _article(self.name).capitalize(), _words(self.name)
        if e.from_state is e.to_state:
            move = f"{art} {noun} can re-enter {_label(e.to_state)}"
        else:
            move = f"{art} {noun} can move from {_label(e.from_state)} to {_label(e.to_state)}"
        if e.action:
            move += f" by {_words(e.action)}"
        if e.guards:
            move += ", only when " + " and ".join(g.description for g in e.guards)
        return move + "."

    def catalog(self) -> dict[str, Any]:
        """Everything about this lifecycle, machine-readably and JSON-safe:
        states with their flags and next states, every transition with its
        action, guards and sentence, and the full description."""
        return {
            "name": self.name,
            "description": self.description,
            "enum": self._enum.__name__,
            "states": [
                {
                    "value": s.value,
                    "initial": s in self._initial,
                    "terminal": s in self._terminal,
                    "next": [t.value for t in self.allowed_from(s)],
                    "actions": sorted({e.action for e in self._out[s] if e.action}),
                }
                for s in self._states
            ],
            "initial": [s.value for s in self._states if s in self._initial],
            "terminal": [s.value for s in self._states if s in self._terminal],
            "transitions": [{**e.as_dict(), "sentence": self._sentence(e)} for e in self._edges],
            "actions": sorted({e.action for e in self._edges if e.action}),
            "records_history": self.sink is not None,
            "sentences": self.describe(),
        }

    def __repr__(self) -> str:
        return f"Lifecycle({self.name!r}, {self._enum.__name__}, {len(self._edges)} transitions)"


# ── the process-wide catalog ────────────────────────────────────────────────

_lock = threading.Lock()
_registry: dict[str, Lifecycle[Any]] = {}


def _shape(lc: Lifecycle[Any]) -> dict[str, Any]:
    shape = lc.catalog()
    shape.pop("records_history")
    return shape


def _register(lc: Lifecycle[Any]) -> None:
    with _lock:
        prior = _registry.get(lc.name)
        if prior is not None and _shape(prior) != _shape(lc):
            raise DefinitionError(lc.name, [Problem(
                "duplicate_name",
                f"another lifecycle named {lc.name!r} with a different definition "
                "already exists; names are unique per process (pass register=False "
                "for a private one)",
            )])
        _registry[lc.name] = lc


def lifecycles() -> dict[str, Lifecycle[Any]]:
    """Every registered lifecycle, by name. Construction registers, so there is
    nothing to call."""
    with _lock:
        return dict(sorted(_registry.items()))


def get(name: str) -> Lifecycle[Any]:
    """The registered lifecycle called ``name``; ``KeyError`` naming the known
    ones otherwise."""
    with _lock:
        try:
            return _registry[name]
        except KeyError:
            raise KeyError(
                f"no lifecycle named {name!r}; known: {', '.join(sorted(_registry)) or 'none'}"
            ) from None


def catalog() -> list[dict[str, Any]]:
    """:meth:`Lifecycle.catalog` for every registered lifecycle, sorted by name:
    the one call an agent makes to learn every status model in the process."""
    return [lc.catalog() for lc in lifecycles().values()]


def reset() -> None:
    """Forget every registered lifecycle (test isolation)."""
    with _lock:
        _registry.clear()


# ── words ───────────────────────────────────────────────────────────────────


def _as_many(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (str, Enum)):
        return [value]
    if isinstance(value, Iterable):
        return list(value)
    return [value]


def _words(text: str) -> str:
    return str(text).replace("_", " ").replace("-", " ")


def _label(state: Enum) -> str:
    return _words(state.value)


def _article(noun: str) -> str:
    return "an" if str(noun)[:1].lower() in "aeiou" else "a"


def _or(items: list[str]) -> str:
    items = list(items)
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " or " + items[-1]
