# Changelog: `asas-lifecycle`

Versions follow semver, and the git tag matches this file: `asas-lifecycle/v0.1.0`.
Pre-1.0, a breaking change bumps the **minor**.

Release procedure and the historical tag mapping: [`RELEASING.md`](../../RELEASING.md).

## 0.1.0 (unreleased)

First release. Extracted from a production system whose status-bearing
records each carried a hand-rolled
`_TRANSITIONS` dict with its own `can_transition`, `guard_transition` and
`is_terminal`, raising an error whose only structure was an English sentence.

- **`Lifecycle`** over the host's own `StrEnum`: initial states, terminal
  states, and edges as tuples or `Edge(from, to, action, guard=...)`, where
  `from` may be several states and one pair may carry several named actions.
- **Validated at construction.** One `DefinitionError` lists every problem with
  a code: unknown states, unreachable states, terminal states with exits,
  non-terminal dead ends, duplicate edges, ambiguous actions, guards without a
  sentence.
- **`transition`, `fire`, `start`, `can_transition`, `allowed_from`,
  `available`, `edges_from`, `is_terminal`.** Refusals are `InvalidTransition`
  (reason `not_allowed`, `from_terminal`, `unknown_action`, `not_initial`) or
  `GuardRefused`, each carrying from, to, action and the allowed next states,
  with `as_dict()` and an optional FastAPI `to_http()` (409).
- **Guards** are host callables given the caller's context, each with a name
  and a sentence.
- **`describe()` and `catalog()`** per lifecycle, and a process-wide
  `catalog()` fed by construction (no registration call).
- **History is a host sink**, not a package table: `sink=` receives each
  `TransitionRecord` with the caller's context passed through.
- No dependencies; FastAPI only for the optional `to_http`.
