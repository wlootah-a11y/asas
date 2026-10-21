# Changelog — `asas-graph`

Versions follow semver, and the git tag matches this file: `asas-graph/v0.1.1`.
Pre-1.0, a breaking change bumps the **minor**.

Release procedure and the historical tag mapping: [`RELEASING.md`](../../RELEASING.md).

## 0.1.1 (unreleased)

Free/busy and the domain-free core of a slot finder, ported from a production
scheduling service. Additive: nothing in 0.1.0 changes.

- **`FreeBusyReader(client, mailbox).get_schedule(mailboxes, start, end,
  interval_minutes=60)`** over Graph `getSchedule`, returning one
  **`MailboxSchedule`** per requested address (`readable`, `intervals` of
  **`BusyInterval`**, `busy`, `working_hours`, `availability_view`, `error`).
  Kept from the engine: the fail-closed rules (unknown status raises, errored,
  omitted or detail-withheld mailboxes are unreadable and busy for the whole
  window, a non-UTC answer is refused, `scheduleId` matched
  case-insensitively, half-read working hours are `None`). New here: lists
  over Graph's 100-address cap are split into batches (`max_per_request`)
  rather than refused, duplicates go on the wire once, `interval_minutes` is
  a parameter, and the 7-digit fractional seconds Graph sends are trimmed so
  Python 3.11 parses them too.
- **`FreeBusyStatus`**, **`WorkingHours`** (`zone()`, `iso_days()`, `usable`)
  and **`resolve_time_zone`**, which reads IANA names and the Windows ids
  Graph actually sends (`Arabian Standard Time`).
- **`SlotFinder(step, default_working_hours, use_mailbox_hours).find(schedules,
  start, end, duration, limit, non_overlapping)`** returns **`Slot`**s: the
  step-aligned windows everyone passed in is free for, with each mailbox's
  own hours in its own zone and a default for the rest. The engine's
  ranking (reviewed-window tiers, fragmentation score, optional-attendee
  conflicts, rationale text, near-miss explanation, notice period, holiday
  and business-day counting) is the product's scheduling policy and stayed
  with the product.
- **`FreeBusyError`** (with `is_transient`) and
  **`UnmappedFreeBusyStatusError`** join the `GraphError` hierarchy.

## 0.1.0 — unreleased

First release. Extracted from the AI Recruiter engine's `base/services/graph`
(`GraphClient`, `TeamsMeetingService`) and generalised — the shapes below are
the ones that survived production, with the product-specific parts removed.

- **`GraphSettings`** replaces the engine's `config.GRAPH_*` import: an explicit
  object the host builds, validated at construction (`GraphConfigError`), with
  `authority_host`/`base_url` for national clouds and an opt-in `from_env()`.
- **`GraphClient`** is a plain object rather than a process singleton, owns (or
  borrows) one `httpx.AsyncClient`, verifies TLS against certifi's bundle, and
  returns `None` for empty successes (202/204) instead of needing a separate
  `post_no_content`. Sits on a **`TokenProvider`** seam: MSAL client-credentials
  by default (`ClientCredentialTokenProvider`), `StaticTokenProvider` for tests
  and for hosts with their own token source.
- **`GraphRequestError`** now unpacks Graph's error envelope (`code`,
  `message`, `request_id`) and the `Retry-After` header (`retry_after`,
  `is_throttled`, `is_transient`, `is_not_found`). No `status_code=502` — how a
  host reports a Graph failure to its own callers is host policy.
- **`TeamsMeetings`** (`create`/`reschedule`/`cancel`) takes the organiser
  explicitly. Datetimes must be timezone-aware and are converted to UTC — the
  engine formatted whatever it was given and labelled it UTC, which is wrong for
  any non-UTC aware datetime. `end <= start` is refused. `Attendee` gains
  `required=False` for optional attendees; `Meeting` gains `web_link`.
- Depends on `httpx`, `msal`, `certifi` only. No pydantic, no FastAPI.
