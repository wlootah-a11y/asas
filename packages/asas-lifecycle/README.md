# asas-lifecycle

A small typed state machine for record statuses. You declare, next to your own
status `StrEnum`, which moves are allowed, which states a record starts in and
which are final. The definition is checked when it is built, and your services
ask it one question before they write: may this record go from here to there?

This is the thing *below* `asas-workflow`. Workflow runs approval processes
(instances, assignees, quorums, decisions, nine tables). A lifecycle is one
record's status and its legal moves, with no tables at all. If a status change
needs a vote, reach for workflow; if it only needs to be legal, this is enough.

Table-less **and** router-less variant of the Asas host contract: no session
dependency, no `seed`/`migrate`/`build_routers`, no dependencies.

## Quickstart

```
asas-lifecycle @ git+https://github.com/wlootah-a11y/asas.git@asas-lifecycle/v0.1.0#subdirectory=packages/asas-lifecycle
```

```python
from enum import StrEnum
from asas_lifecycle import Edge, Lifecycle, guard

class JobStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    CLOSED = "closed"

@guard("the job has never received an application")
def never_applied(ctx) -> bool:
    return ctx["applications"] == 0

JOB = Lifecycle(
    "job",
    JobStatus,
    initial=JobStatus.DRAFT,
    terminal={JobStatus.CLOSED},
    transitions=[
        (JobStatus.DRAFT, JobStatus.ACTIVE, "publish"),
        (JobStatus.DRAFT, JobStatus.CLOSED, "discard"),
        Edge(JobStatus.ACTIVE, JobStatus.DRAFT, "unpublish", guard=never_applied),
        (JobStatus.ACTIVE, JobStatus.CLOSED, "close"),
    ],
)

JOB.transition(JobStatus.DRAFT, JobStatus.ACTIVE)     # a TransitionRecord, or raises
JOB.fire("active", "unpublish", context={"applications": 0})
JOB.can_transition("active", "draft", context={"applications": 3})  # False
JOB.allowed_from(JobStatus.DRAFT)                     # (ACTIVE, CLOSED)
```

That is the whole adoption: one declaration, one call where the service
already changes the status.

## What you get

- **`transition(from, to, *, action=None, context=None)`** validates the move,
  runs its guards against `context`, and returns a `TransitionRecord`
  (`lifecycle`, `from_state`, `to_state`, `action`, `context`). Without
  `action`, the first declared edge between the two states whose guards pass is
  used, and the record says which.
- **`fire(from, action)`** applies a named move; the lifecycle knows where it
  leads.
- **`start(state=None)`** is the creation event: `from_state` is `None`, and
  the state must be an initial one (omit it when there is only one).
- **`can_transition`** answers the same question without recording anything.
  **`allowed_from(state)`** gives the next states (guards ignored);
  **`available(state, context)`** gives the edges whose guards pass, which is
  what a UI or an agent should offer. **`is_terminal`**, **`edges_from`**,
  **`states`**, **`initial`**, **`terminal`**, **`transitions`**.
- **Edges** are `(from, to)`, `(from, to, action)` or `Edge(from, to, action,
  guard=..., description=...)`. `from` may be several states: `Edge((NEW,
  REVIEW, RETURNED), CANCELLED, "cancel")` is one line for "cancellable before
  it goes live". Self-edges (`SCHEDULED -> SCHEDULED` for a reschedule) are
  fine. Two actions may join the same pair (`discard` and `hire` both close a
  job).
- **Guards** are host callables given the caller's `context`. Each needs a name
  and a sentence: `@guard("...")`, `Guard(name, check, description)`, or a
  plain function whose docstring's first line is the sentence. Several guards
  on one edge must all pass; the first that fails is the one reported.

## Refusals are data

Every error is a `LifecycleError` with a stable `code` and `as_dict()`:

```python
from asas_lifecycle import InvalidTransition

try:
    JOB.transition("draft", "draft")
except InvalidTransition as err:
    err.as_dict()
# {"code": "invalid_transition", "message": "A job cannot move from draft to draft",
#  "lifecycle": "job", "from": "draft", "to": "draft", "action": None,
#  "allowed": ["active", "closed"], "reason": "not_allowed"}
```

`reason` is `not_allowed`, `from_terminal`, `unknown_action`, `not_initial`, or
`guard_refused`; the last comes as `GuardRefused`, a subclass that adds `guard`
and `guard_description`. A value that is not a state at all raises
`UnknownStateError` from every method, `can_transition` included: a typo is a
bug, not a "no". `to_http(err)` builds a FastAPI 409 with that body when
FastAPI is installed (`asas-lifecycle[fastapi]`); the package itself imports
nothing.

## Definitions fail loud

Construction raises one `DefinitionError` listing every problem, each with a
code (`err.codes`):

| code | meaning |
| --- | --- |
| `unknown_state` | an initial, terminal or edge state is not a member of the enum (strings are matched to values; another enum's member is refused) |
| `unreachable_state` | no path reaches it from an initial state |
| `terminal_has_exits` | a terminal state has an outgoing edge |
| `dead_end` | a non-terminal state has no way out (declare it terminal) |
| `duplicate_edge` | the same from, to and action twice |
| `ambiguous_action` | one action from one state leads to two places |
| `guard_undescribed` | a guard without a name or a sentence (a bare lambda) |
| `no_initial`, `invalid_edge`, `invalid_action`, `invalid_guard`, `states_not_enum`, `state_value_not_str`, `invalid_name`, `duplicate_name` | the rest |

## Self-describing

```python
JOB.describe()
# ["A job starts in draft.",
#  "A job can move from draft to active by publish.",
#  "A job can move from draft to closed by discard.",
#  "A job can move from active to draft by unpublish, only when the job has never received an application.",
#  "A job can move from active to closed by close.",
#  "Closed is final: a job never leaves it."]

JOB.catalog()            # states with flags, next states and actions; every
                         # transition with guards and its sentence; JSON-safe
asas_lifecycle.catalog() # every lifecycle in the process
```

Building a lifecycle registers it under its name, so there is nothing to call
for the process catalog. Rebuilding the same definition (a module reload) is
fine; a different definition under a taken name raises `duplicate_name`. Pass
`register=False` for a private one; `reset()` clears the registry in tests.

## History is yours

The package owns no table. Pass `sink=` a callable and it receives each
`TransitionRecord` after the move is validated and before `transition`
returns; `context` passes through untouched, so the sink can reach your
session, actor and tenant:

```python
def record_stage(event):
    ctx = event.context
    ctx["session"].add(ApplicationStageEvent(
        application_id=ctx["application_id"], actor=ctx["actor"],
        from_stage=event.from_state, to_stage=event.to_state,
        detail={"action": event.action},
    ))

APPLICATION = Lifecycle(..., sink=record_stage)
```

A refused move calls nothing. Whatever the sink raises propagates, so the
status write and the history row succeed or fail together in your transaction.
In async code, skip the sink and `await` your own write with the returned
record.

Why no package table: a history row is only useful when it points at the
record and the tenant, rides the same transaction as the status change, and
cascades when the record goes. A package table could do none of that under the
Asas rules (no host foreign keys, no host imports), would be a second history
beside the audit log the host already keeps, and would make leaving the
package a migration instead of a deleted import (principle 10, "no second
platform", and the "cheap to leave" promise).

## Not in scope

No persistence, no clock, no async, no approvals or assignees (`asas-workflow`
does those), no per-state permissions (`asas-access`), no runtime
configuration of the graph: transitions are code, reviewed and tested
(principle 9).

Extracted from a production system that carried five near-identical copies of
this. `tests/test_parity.py` rebuilds five such machines (renamed into neutral
domains) and checks every pair of states against the hand-rolled tables.
