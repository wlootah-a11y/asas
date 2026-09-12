# Changelog — `asas-validation`

Versions follow semver, and the git tag matches this file: `asas-validation/v0.11.1`.
Pre-1.0, a breaking change bumps the **minor**.

Release procedure and the historical tag mapping: [`RELEASING.md`](../../RELEASING.md).

## 0.11.1 — 2026-09-12

Additive release (DR 0004 phase 1): the **check library**. The declared-rules
engine is untouched; everything below is new surface.

- **44 checks as plain functions** in six families (dates/time, numbers,
  cross-field, presence, formats, lists), each named to read as the rule it
  enforces, returning `None` or a `Violation`. Absent values pass (partial
  edits), presence checks excepted.
- **`validate`** — the namespace with two doors: `validate.after(a, b, days=3)`
  for humans (real functions, IDE-visible signatures) and
  `validate("after", ...)` for callers holding the name as data. Unknown names
  raise with the known-checks list.
- **`enforce(results)`** — drops passes, raises one 422 carrying every
  violation; envelope unchanged, plus additive `message_key`/`params` keys on
  check violations for translating clients.
- **`include_checks(module)`** — hosts add checks by writing functions in
  their own file; name collisions with existing checks fail loud.
- **`catalog()`** — machine-readable list of every check (takes, settings,
  sentence), derived from the functions by introspection.
- **`configure_clock(fn)`** — the temporal checks and the declared-rules
  engine obtain "today" through a host-configurable clock (startup-only; a
  per-request-timezone host installs one callable that reads its own request
  context). Date-meets-datetime comparisons coerce instead of raising, in
  the checks and in the engine's kind evaluators alike (calendar-naive
  truncation — hosts needing timezone-exact day boundaries pass dates).
- Non-presence checks treat the empty string like ``None`` — absent, so a
  cleared form field or CSV cell skips the check instead of crashing a
  comparison. ``after``/``before`` are strict at ``days=0`` (equality
  violates, as the names promise); with a gap, "at least N days" stays
  inclusive at exactly N.
- `Violation` gains optional `params` and `message_key` fields (defaults keep
  every existing construction and payload identical).
- Internal: the known-fields module `catalog.py` was renamed `fields.py` so
  the new `catalog()` callable cannot shadow a submodule (the host-contract
  trap pinned by TEAMY-798). Public imports (`register_fields`, `is_known`,
  `known_fields`) are unchanged.

## 0.11.0 — 2026-08-25

- Licensed under **Apache 2.0** (was proprietary/all-rights-reserved). `LICENSE` and `NOTICE` ship inside the wheel and the metadata carries `License-Expression: Apache-2.0` (Teamy TEAMY-797).
- Added `tests/test_host_contract.py`: `__all__` declared and resolving, contract names callable rather than shadowed by a submodule, module exports declared deliberately (Teamy TEAMY-798).

## Before 2026-08-25

Earlier releases were cut as **repo-wide** tags (`v0.1.0` … `v0.15.0`) under the
lockstep scheme in DR 0017, which decayed: from `v0.11.0` onward the repo tag no
longer matched any package's own version, so `asas-validation @ v0.15.0` did not install
`asas-validation` 0.15.0. `RELEASING.md` carries the full tag-to-version table for
decoding an old pin. Individual changes are in the git history.
