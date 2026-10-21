# asas-graph

Microsoft Graph for a Microsoft 365 tenant, app-only: sign in **as the
application** (client credentials), call Graph with typed errors, and book,
move and cancel Teams meetings in an organiser's calendar. The certificate
trust-store problem ("unable to get local issuer certificate" on machines whose
OpenSSL bundle is empty) is solved once, here.

Nothing in it is about any one product. It is an **AI-tier** Asas
package: it fills none of the four host-contract slots (no routers, no schema,
no seeding, no `configure_*` globals). Everything is an object the host
constructs and owns, so a process that talks to two tenants makes two clients,
and a test passes a fake transport instead of patching a module.

```python
import asas_graph

# boot (host wiring) — build settings from *your* configuration; the library reads none
graph = asas_graph.GraphClient(asas_graph.GraphSettings(
    tenant_id=settings.graph_tenant_id,
    client_id=settings.graph_client_id,
    client_secret=settings.graph_client_secret,
))
teams = asas_graph.TeamsMeetings(graph, organizer="interviews@example.gov")

# use
meeting = await teams.create(
    "Cloud Architect interview",
    start, end,                                   # timezone-aware datetimes
    attendees=[asas_graph.Attendee("chen.wei@example.com", "Chen Wei")],
    body_html="<p>Agenda…</p>",
)
meeting.id        # the calendar event id — keep it; reschedule/cancel need it
meeting.join_url  # the Teams join link

await teams.reschedule(meeting.id, new_start, new_end)
await teams.cancel(meeting.id)

# shutdown
await graph.aclose()          # or: async with asas_graph.GraphClient(...) as graph:
```

## What a host needs in the tenant

An app registration with a client secret and the **`Calendars.ReadWrite`
application** permission, admin-consented. That is all: the meeting is a
calendar event with `isOnlineMeeting=true`, so Exchange sends the invitations,
updates and cancellations itself off the calendar write. The library sends no
mail and needs no `Mail.Send` (that is `asas-mail`'s job, and it will sit on
this client). `OnlineMeetings.ReadWrite.All` is **not** needed.

`organizer` is the mailbox the events live in — a shared mailbox such as
`interviews@example.gov` works well. It must be a real, licensed mailbox.

## Settings

`GraphSettings(tenant_id, client_id, client_secret, authority_host=…, base_url=…,
scopes=…, timeout_seconds=30)`. It validates at construction, so a
misconfigured host fails at boot, not at its first meeting.

`authority_host` and `base_url` exist for national clouds — Azure Government
signs in at `login.microsoftonline.us` and calls `graph.microsoft.us`; the
defaults are the global cloud. `GraphSettings.from_env()` reads
`GRAPH_TENANT_ID` / `GRAPH_CLIENT_ID` / `GRAPH_CLIENT_SECRET` (plus the optional
`GRAPH_AUTHORITY_HOST`, `GRAPH_BASE_URL`, `GRAPH_SCOPES`, `GRAPH_TIMEOUT_SECONDS`)
for hosts configured by environment; the prefix is a parameter.

## The client

`GraphClient.get / post / patch / delete` take a path relative to `base_url`
(`/users/{id}/events`) and return the decoded JSON body, or `None` when the
success carried none — `sendMail` answers 202, `PATCH` and `DELETE` answer 204,
and a first implementation usually crashes parsing the body that is not there.

Any 4xx/5xx raises `GraphRequestError` with `status`, `method`, `path`, the
decoded body as `detail`, and Graph's error envelope unpacked: `code`,
`message`, `request_id` (what Microsoft support asks for). `retry_after` is
set from the `Retry-After` header on throttling; `is_throttled`,
`is_not_found` and `is_transient` say what a caller should do. The library
does **not** retry: how many times, and whether to wait, is host policy.

TLS is verified against `certifi`'s bundle (`default_ssl_context()`), which is
the fix for the empty-trust-store failure. A host that must trust a private CA,
go through a proxy, or share a connection pool passes its own
`httpx.AsyncClient` as `http=`; the library uses it as-is and does not close it.

## Tokens

The client asks a `TokenProvider` (`async access_token() -> str`) before every
request and caches nothing itself. The default is `ClientCredentialTokenProvider`:
MSAL's confidential-client flow, one MSAL application per provider so MSAL's
in-memory cache is honoured and the token endpoint is hit only near expiry.
`StaticTokenProvider(token)` is for tests and for hosts that get tokens
elsewhere. When the shared service-token client (`asas-upstream`) exists, it
plugs in at this seam and nothing here changes.

## Meetings

`TeamsMeetings(client, organizer)` → `create`, `reschedule`, `cancel`.

- **Datetimes must be timezone-aware.** They are converted to UTC and sent with
  `timeZone: "UTC"`; Graph renders each attendee's copy in *their* zone. A
  naive datetime raises `ValueError` rather than being guessed at.
- `create(..., online=False)` books a plain calendar event with no Teams link.
- `reschedule` sends only what you pass: the window always, `subject` and
  `attendees` only when given. Passing `attendees=[]` clears them. Every PATCH
  notifies attendees and resets their responses, so compare before you call it.
- `cancel` deletes the organiser's event; Exchange sends the cancellation.
- Failures raise `MeetingCreationError` / `MeetingUpdateError` /
  `MeetingCancelError` (all `MeetingError`), with the `GraphRequestError` as
  `__cause__`.

## Free/busy

`FreeBusyReader(client, mailbox)` reads free/busy over Graph `getSchedule`
(`POST /users/{mailbox}/calendar/getSchedule`). `mailbox` is the user the call
runs under: app-only there is no `/me`, so any licensed mailbox the
permission covers will do, typically the same service mailbox that organises
meetings. The `Calendars.ReadWrite` application permission the meetings
already need covers it, working hours included (proven on a live tenant by the
engine); `MailboxSettings.Read` is not needed. Microsoft lists lower read-only
calendar permissions for `getSchedule` too; they are untested here. `findMeetingTimes` is not used
because it has no application-permission form.

```python
reader = asas_graph.FreeBusyReader(graph, mailbox="meetings@example.gov")
schedules = await reader.get_schedule(
    ["chen.wei@example.com", "sara@example.com"],
    start, end,                     # timezone-aware; sent to Graph in UTC
    interval_minutes=60,            # availabilityViewInterval, 5..1440
)
for s in schedules:                 # one per requested address, in your order
    s.mailbox, s.readable, s.busy, s.working_hours, s.error
```

It reads fail-closed, which is what the engine it came from learned on a live
tenant:

- **Batching.** Graph refuses more than 100 addresses per request
  (`ErrorMailboxDataArrayTooBig`). Longer lists are split into batches of
  `max_per_request` (default and maximum 100), sent one after another.
  Addresses are matched case-insensitively (Graph echoes the mailbox's own
  casing as `scheduleId`) and a repeated address is sent once.
- **Per-mailbox errors do not fail the call.** An `error` entry, an address
  Graph leaves out, or a mailbox that shares availability but withholds the
  intervals behind it comes back as `readable=False` with one `unknown`
  interval over the whole window and the reason in `error`. A caller that
  ignores the flag still cannot book it.
- **Whole-request errors raise `FreeBusyError`**, with the
  `GraphRequestError` (or the transport error) as `__cause__` and
  `is_transient` set for throttling, 5xx and dropped connections. With
  several batches the answer is all or nothing.
- **Statuses.** `FreeBusyStatus` is `free`, `tentative`, `busy`, `oof`,
  `workingElsewhere`, `unknown`. Everything but `free` is in `busy`. A status
  outside that set raises `UnmappedFreeBusyStatusError`, never reads as free.
- **Time zones.** The window goes out in UTC and the answer is read as UTC;
  an answer labelled with any other zone raises rather than being shifted
  silently. `BusyInterval.start/end` are aware UTC datetimes. Working hours
  keep the zone Graph names, which is a **Windows** id such as
  `Arabian Standard Time`; `WorkingHours.zone()` (and `resolve_time_zone`)
  resolves IANA and common Windows names, `None` for anything else. Hours
  missing days or a zone read as `None` rather than half-filled.
- The `availabilityView` string is kept as `availability_view`, but the
  intervals are authoritative: Graph writes `workingElsewhere` as `0` (free)
  in the view. Subjects and locations in `scheduleItems` are never read.

## Common free slots

`SlotFinder` turns schedules into candidate slots, deterministically: exact
interval arithmetic, no clock read, no model call, same input same output.

```python
finder = asas_graph.SlotFinder(
    step=timedelta(minutes=15),                  # the grid
    default_working_hours=asas_graph.WorkingHours(
        ("sunday", "monday", "tuesday", "wednesday", "thursday"),
        time(8), time(16), "Asia/Dubai"),        # for mailboxes that expose none
)
slots = finder.find(schedules, start, end, timedelta(minutes=45),
                    limit=4, non_overlapping=True)
```

A slot is returned when every schedule passed in is free for all of it:
every non-free interval blocks (`tentative` and `unknown` included, so an
unreadable mailbox blocks everything), and so does time outside each
mailbox's working hours, read in that mailbox's own zone (overnight shifts
included). A mailbox's own hours apply where usable, else
`default_working_hours`, else only the window bounds it;
`use_mailbox_hours=False` applies the default to everyone. The grid is
anchored to local midnight of `start` in `start`'s own zone, so a 15-minute
step on a `+04:00` window lands on :00/:15/:30/:45 Dubai time. Slots come back
earliest first, in UTC; `non_overlapping=True` drops a slot that overlaps one
already returned.

It does not **rank**. Which free slot to offer first (preferred windows,
diary fragmentation, busy optional attendees, notice periods, holidays) is
product policy and stays in the host. Pass only the mailboxes that must be
free; check optional ones against the returned slots yourself.

## Errors

```
GraphError
├── GraphConfigError        the host wired it wrong (raised at construction)
├── GraphAuthError          no token could be acquired
├── GraphRequestError       Graph answered 4xx/5xx
└── MeetingError
    ├── MeetingCreationError
    ├── MeetingUpdateError
    └── MeetingCancelError
```

None of these carries an HTTP status for *your* callers; mapping a Graph
failure onto your API's responses is host policy.

## Testing a host

Pass `http=httpx.AsyncClient(transport=httpx.MockTransport(handler))` and
`token_provider=StaticTokenProvider("x")`; nothing leaves the process. The
package's own `tests/conftest.py` has a recording fake worth copying.

See the repo README for the family contract. Extracted from the AI Recruiter
engine's `base/services/graph` (roadmap row 7).
