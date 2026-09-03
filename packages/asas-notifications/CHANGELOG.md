# Changelog — `asas-notifications`

Versions follow semver, and the git tag matches this file: `asas-notifications/v0.15.0`.
Pre-1.0, a breaking change bumps the **minor**.

Release procedure and the historical tag mapping: [`RELEASING.md`](../../RELEASING.md).

## 0.17.0 — 2026-09-03

### Routing is a (topic × urgency) matrix, and `nature` is not a condition

**The cell that could not be written.** A CHECK let a policy row state a topic
OR an axis condition, never both, so "interview notifications, but only the
urgent ones, go to email" had nowhere to live — the nearest storable rules were
"all interview notifications" or "all urgent notifications", and neither is what
an administrator means. A topic row was also never compared against urgency at
all, so the closest available rule applied more widely than intended.

The coordinates are independent now, and NULL is a wildcard:

| `(topic, urgency)` | the rule |
| --- | --- |
| `("interviews", "high")` | this topic at this urgency — **new** |
| `("interviews", None)` | this topic, every urgency |
| `(None, "high")` | every topic at this urgency |
| `(None, None)` | every notification — the org-wide default, also new |

Per channel the most specific match wins: two coordinates beat one, one beats
the all-NULL default, that beats the built-in fallback (`low` → in-app only,
else in-app + email). Org rows still beat platform rows, ties still resolve to
the newest row.

### `reason` leaves the notification row

It answered "why THIS recipient" — GitHub's participating-vs-watching,
generalised. It never chose a channel; it was reserved for the preference layer
(U-3) that was never built. Removed with `nature` so the model carries the two
axes that decide something (`topic`, `urgency`) plus `nature` for presentation,
and nothing that decides nothing.

- **`notify()` no longer accepts `reason=`**, and no longer requires it. The
  required axes are `nature` and `urgency`, plus `topic` whenever `action` is
  given.
- **`notification.reason` is dropped** (migration `0008`).
- **`NotificationRead.reason` and `DeliveryPayload.reason` are gone.** An
  adapter or client reading either needs a change.
- `register_kind(reason=…)` still accepts the argument and ignores it, so a 0.15
  wiring keeps working for this shim's last release.

**What this forecloses.** "Stop emailing me about things I am only watching" was
the preference this axis existed for. It was never implemented, so nothing
working breaks — but the option is closed rather than postponed, and re-adding
the column later cannot reconstruct the value for rows already written. The
downgrade restores the column and backfills `participant`, which is a default
and not a recovery.

### Breaking

- **`resolve_channels` no longer takes `nature=`.** Drop the argument.
- **`notification_channel_policy.nature` is dropped** (migration `0007`), and
  with it the `ck_notification_channel_policy_one_condition` CHECK.
- **A rule whose only condition was `nature` is DELETED**, not widened, and the
  count is logged. With the column gone its condition would vanish and the row
  would begin matching every notification — a silent widening is worse than a
  rule an operator has to rewrite.

**What this genuinely costs.** Two notifications that differ *only* in nature now
route identically. The shipped example was "a warning emails even at low urgency
while an info does not", and that is no longer expressible: urgency cannot
separate them (both are low), so such a rule has to be restated with a topic.
`nature` keeps its place on the `notification` row, where it drives the UI
treatment and the email subject — only its role in choosing a channel ends.

The downgrade restores the column and the CHECK. It cannot un-delete rows or
split a two-coordinate cell into a shape the old constraint accepts, so it
deletes those cells and the all-NULL rows and says so.

## 0.16.0 — 2026-09-03

Implements DR 0003 (U-1 + U-2): action-referenced notifications and axis-based,
data-driven routing. **Breaking** (pre-1.0 minor); every rename below keeps a
one-release deprecated alias where the DR promises one.

- **`kind` is gone; emits reference the application action.** `notify()` takes
  `action=` — the app's own `entity.verb` id, a reference declared nowhere —
  plus the four axes `topic=` / `nature=` (was `category`) / `urgency=` /
  `reason=`. `action` is nullable: ad hoc emits carry axes only and never
  coalesce. Coalescing keys on (org, recipient, action, entity).
- **`register_kind()` is a deprecated shim** (one release): a registered kind
  supplies axis defaults for emits that pass its name, with `topic` defaulting
  to the seeded `general` topic. `notify(kind=...)`/`notify(category=...)`
  warn and map to `action=`/`nature=`.
- **Routing is data-driven** (migration `0004`): new deviation-only tables
  `notification_topic` and `notification_channel_policy` (platform rows +
  org override rows, DR 0001's shared-with-overrides pattern). Per channel,
  most specific wins: topic row → axis row → the built-in fallback, which is
  exactly the 0.15 rule — empty tables reproduce 0.15 routing bit-for-bit
  (equivalence-tested). `in_app` is an explicit channel: a policy that
  disables it suppresses the whole insert. Config reads are TTL-cached (60s;
  `config_cache_clear()` for tests/admin writes). Unknown topics fail loud.
- **Schema** (`0004`): `notification.kind` → `action` (nullable), `category`
  → `nature`, new `topic`, `data` (JSON presentation payload — same PII
  posture as `title`; latest wins on coalesce), `template` (reference stored
  now; rendering lands with U-4). Feed API: `NotificationRead` carries
  `action`/`topic`/`nature`/`template`/`data`; the `?category=` filter remains
  as a deprecated alias for `?nature=` for one release. `service.list_feed`
  filter kwarg is `nature=`.
- **`DeliveryPayload` renamed with the columns** (`action`, `nature`, plus new
  `topic`, `data`) — the payload's one rename; adapters remain
  render-consumers of a channel-agnostic payload.
- Review hardening: the unknown-topic error fires inside `suppressed()` too
  (suppression silences delivery, never catalog mistakes); topic validation
  re-queries fresh on a cache miss (cross-replica seeding lag is one extra
  SELECT, never a false LookupError); policy tie-breaks prefer the newest row;
  the `register_kind` shim covers only fully-legacy calls (any explicit axis
  ⇒ the new fail-loud contract) and warns at the emit site; coalesce folds
  refresh `topic`/`template` alongside `data`; `list_feed` keeps a deprecated
  `category=` alias; migration `0004` builds its `notification` indexes
  CONCURRENTLY on Postgres and its downgrade backfills NULL `action` rows;
  the adoption guard recognizes the post-rename schema instead of advising
  the destruction of real data.

### A host whose keys are not integers is now actually tested

`normalize_id` is the identity function for a value that is already a string, so
a test that passes a UUID cannot tell a working conversion from a missing one —
and every test in this package passed integers, which is the case where the
conversion does something. Two defects reached a consumer through that gap:

- `_owned` compared the stored value against the host's own in Python rather
  than in SQL, giving `"1" != 1`, so **every** read of a notification answered
  404 to its own recipient.
- The coalesce query converted four of its five comparands and not `entity_id`,
  which SQLite accepted by column affinity and Postgres correctly refused, so a
  burst stopped folding on the dialect that matters.

Both were fixed when found, and both are pinned: reverting the first fails 16
tests, the second 6 — the latter **only on Postgres**, which is why the CI matrix
runs both dialects and why a green SQLite run is not evidence here.

New `test_opaque_identity.py` covers the string-host path itself, which had none:
ownership, archive, cross-org probes, coalescing and its org axis, the feed and
unread count, actor exclusion, what reaches an adapter, and the two config
tables. Plus the int-host contract, stated rather than assumed — an int host
keeps passing ints and reads back their decimal strings.

Four annotations still said `int` for values that are now the host's own
identity (`_PolicyRow.org_id`, `_policy_rows`, `resolve_channels`,
`notify(actor_user_id=)`). No behaviour change; they contradicted the release.

### The recipient's language, recorded at emit

- **`configure_locale_resolver(fn)`** is a new host seam, `(session, user_id) ->
  language tag | None`, asked once per **recipient** at emit. Optional, and a
  no-op when unconfigured, so nothing changes for a single-language deployment.
- **`notification.locale`** (migration `0006`) is nullable with no backfill and
  no default. NULL means the host wired no resolver, which is what every
  existing row means and what every existing deployment already does.
- **`notify(..., locale=)`** overrides the resolver, for a producer that already
  holds the answer or a digest job rendering one language deliberately.
- **`DeliveryPayload.locale`** carries it to an adapter, and dispatch selects and
  forwards it. Stamping a value nothing downstream can read would achieve
  nothing.

**Why at emit and not at dispatch.** `dispatch_pending` runs on raw connections
outside any request, where the context resolver returns `None` by contract, so a
renderer between the outbox and an adapter has nobody to ask what language a
recipient reads. A notification emitted today and mailed by tomorrow's sweep
would render in the deployment default, which for a reader of the other language
is simply the wrong email.

**Asked per recipient, not per emit.** One `notify` fans out to people who read
different languages; resolving once would hand everyone the first recipient's.
The host receives its own `user_id` value, not the stored form, for the same
reason the recipient filter does.

### Identity columns are opaque strings

**What a consumer must do differently**
- **`org_id`, `user_id` and `entity_id` are opaque strings.** They were `int`,
  which reads as decoupling and is not: an integer column is an assertion about
  the host's schema, namely that it numbers its users and organisations
  sequentially. A host on UUID primary keys had nothing to put there and no seam
  widened it, so it could not adopt the package at all. Migration `0005` casts
  existing values in place and every index keeps its column list and order.
- **Host code that reads `n.user_id` and compares it to an integer must compare
  to a string.** An int host keeps PASSING ints (`normalize_id` coerces at the
  boundary) and reads back their decimal form, so the write path needs no
  change; only a read that compares does.
- **Test this on the engine you deploy on.** SQLite's column affinity coerces
  `user_id == 1` against a text column and keeps working, while Postgres raises
  `operator does not exist: character varying = integer`. Six tests in this
  package's own suite were green on SQLite and red on Postgres while this was
  being written: four from a comparison inside the package that had been missed,
  and two from the tests' own `where(Notification.user_id == 1)`. A suite that
  runs only on SQLite will not show you this.
- **The visibility filter and the context resolver are deliberately
  unaffected.** They are handed the host's own id values, not the storage form.
  A filter written against ints that silently stops dropping anyone is a leak,
  and that is the one failure that seam exists to prevent.
`DeliveryPayload.recipient_user_id` and `.org_id` are strings for the same
reason: an adapter that looks a user up by integer primary key coerces on its
own side.
## 0.15.0 — 2026-08-28

Re-lands the still-open parts of PR #20 (opened against 0.12.0; its emit-side
org fixes were superseded by 0.13.0's) on top of 0.14.0.

- **Feed pagination moved into SQL.** `GET /me/notifications` previously fetched
  every matching row and sliced the page in Python; it now pages via the new
  `service.list_feed()` (`COUNT` + `LIMIT`/`OFFSET`; also callable directly by
  host jobs). `unread_count()` likewise counts in SQL. Response shape is
  unchanged; `total` and the page are separate statements, so a concurrent
  commit can transiently skew them by a row — the standard trade for SQL
  pagination.
- **Org scoping as defense in depth on the read paths.** 0.13.0 fixed the emit
  side; now, when the configured context resolver supplies an org, every
  feed/read/archive query and per-row ownership check constrains `org_id` in
  addition to `user_id` (a cross-org id probe 404s exactly like a missing row);
  all sites share one `_recipient_conditions` chokepoint. Resolvers are
  consulted on read paths too and must return `None` cheaply outside a request.
  Hosts without a resolver keep user-only scoping; host-level tenancy remains
  the first line.
- **`mark_all_read()` / `archive_read()`** each issue one bulk `UPDATE` instead
  of loading every row and flushing per-row updates.
- **Composite indexes for the hot scans** (migration `0003`):
  `(user_id, org_id, archived_at, created_at, id)` for the feed (id as the
  ORDER BY tiebreaker), `(user_id, org_id, read_at, archived_at)` for the
  badge — `org_id` second so the org-scoped queries filter on the index while
  unscoped single-tenant queries still use the `user_id` prefix — and
  `(status, claimed_at)` for the dispatcher; the single-column `user_id` and
  `status` indexes they subsume are dropped. Every create/drop is guarded by an
  existence check (adopting hosts may have differently-named historical
  indexes), and on Postgres the builds run `CONCURRENTLY` so a boot-time
  `migrate()` never blocks writes to a live table; a name-matching INVALID
  index left by an interrupted concurrent build is detected via
  `pg_index.indisvalid` and dropped + rebuilt rather than silently kept.

## 0.14.0 — 2026-08-27

- **BREAKING: the recipient filter's signature gained `entity_id`.** It is now
  called as `fn(session, user_ids, entity_type, entity_id, record)`. **Action
  for hosts:** add the parameter to your filter.
- **The filter now runs for every `notify` that names an `entity_type`**, not
  only those that also passed `record=`. Filtering on `record is not None` let a
  producer skip the visibility check silently just by not having the row to
  hand — every named recipient was notified, including for a restricted subject,
  and a notification is a *copy*, so there is no redaction pass afterwards.
  `record` is still passed through when the producer has it and is `None`
  otherwise; the id is always passed so the filter can resolve the row itself.
  **Action for hosts:** make sure your filter tolerates `record=None` — an
  entity type that needs no filtering should return `user_ids` unchanged.
- Requiring `record=` at every call site was considered and rejected: a generic
  producer (a workflow-event bridge) legitimately holds only the type and the
  id and cannot load an arbitrary subject. Only the host knows which entity
  types need gating, so the decision belongs in the filter (Teamy TEAMY-807).

## 0.13.0 — 2026-08-27

- **Breaking: an emit with no org fails loud at the emit site** (issue #27,
  audit defect T-2). `Notification.org_id` is NOT NULL, but a `notify()` from a
  background job, CLI, or boot sweep — where the context resolver answers
  `None` — used to insert NULL and die as an engine-specific `IntegrityError`
  at flush, taking the producer's whole transaction with it. Stamping order is
  now: the new explicit `org_id=` parameter → the context resolver → a clear
  `ValueError` raised before any row is staged. Background producers acting
  *for* a tenant pass `org_id=` explicitly; hosts that relied solely on an ORM
  tenancy listener must pass it or configure the resolver.
- **Coalescing never crosses orgs** (defect T-6): the `coalesce_unread` merge
  identity now includes `org_id` — where hosts' entity ids are not globally
  unique, an org-2 emit can no longer fold into (and overwrite) an org-1 row
  for the same (recipient, kind, entity).

## 0.12.0 — 2026-08-25

- **Adoption is now shape-verified.** `migrate()` previously decided adopt-vs-create on the sentinel table's *name* alone, so a host that already owned a table of that name had the baseline stamped as applied and skipped entirely — silently, and unrepairable by re-running. It now requires every baseline table to be present and the sentinel to carry the baseline's columns, and raises with the table, the package and the remedy otherwise (Teamy TEAMY-795).
- Licensed under **Apache 2.0** (was proprietary/all-rights-reserved). `LICENSE` and `NOTICE` ship inside the wheel and the metadata carries `License-Expression: Apache-2.0` (Teamy TEAMY-797).
- Added `tests/test_host_contract.py`: `__all__` declared and resolving, contract names callable rather than shadowed by a submodule, module exports declared deliberately (Teamy TEAMY-798).

## Before 2026-08-25

Earlier releases were cut as **repo-wide** tags (`v0.1.0` … `v0.15.0`) under the
lockstep scheme in DR 0017, which decayed: from `v0.11.0` onward the repo tag no
longer matched any package's own version, so `asas-notifications @ v0.15.0` did not install
`asas-notifications` 0.15.0. `RELEASING.md` carries the full tag-to-version table for
decoding an old pin. Individual changes are in the git history.
