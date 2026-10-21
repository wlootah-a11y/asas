"""Free/busy over Microsoft Graph ``getSchedule``, read fail-closed.

``POST /users/{mailbox}/calendar/getSchedule`` answers, per requested address,
its ``scheduleItems`` (typed busy intervals), an ``availabilityView`` string
and the mailbox's own ``workingHours``, all under the ``Calendars.ReadWrite``
(or ``Calendars.Read``) application permission. ``findMeetingTimes`` is not
used: it has no application-permission form, so it is unusable app-only.

What a production scheduling service learned on the wire, kept here:

- **The cap is 100 addresses per request.** Graph answers
  ``ErrorMailboxDataArrayTooBig`` at 101 (proven against a live tenant). The
  reader splits a longer list into batches of at most ``max_per_request``.
- **Answers are UTC** when the request is sent in UTC and no
  ``Prefer: outlook.timezone`` header is set. Anything else is refused rather
  than stamped UTC: a silent shift books someone over a real meeting.
- **Every status but ``free`` is busy**, ``unknown`` included, and a status
  outside the known set raises :class:`UnmappedFreeBusyStatusError`.
- **Unreadable is not empty.** An ``error`` entry, an address Graph omits, or
  a mailbox that shares availability but withholds the intervals behind it
  (``availabilityView`` marked busy, ``scheduleItems`` empty) comes back with
  ``readable=False`` *and* one ``unknown`` interval over the whole window, so a
  caller that ignores the flag still cannot book it.
- **``scheduleId`` echoes the mailbox's own casing**, so addresses match
  case-insensitively.
- **``workingHours.timeZone.name`` is a Windows zone id** (``Arabian Standard
  Time``), not IANA. :meth:`WorkingHours.zone` resolves both.
- Graph writes ``workingElsewhere`` as ``0`` (free) in ``availabilityView``
  while naming it correctly in ``scheduleItems``, so the intervals are the
  authoritative source and the view is only a tripwire.

Only ``status``/``start``/``end`` are read from ``scheduleItems``; the
``subject`` and ``location`` Graph also sends are never stored or logged.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, time, timezone
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from .client import GraphClient
from .errors import (
    FreeBusyError,
    GraphConfigError,
    GraphRequestError,
    UnmappedFreeBusyStatusError,
)

#: Graph's own cap on ``schedules[]`` (``ErrorMailboxDataArrayTooBig`` above it).
MAX_SCHEDULES_PER_REQUEST = 100
#: Graph accepts ``availabilityViewInterval`` from 5 to 1440 minutes.
_MIN_INTERVAL_MINUTES, _MAX_INTERVAL_MINUTES = 5, 1440
_DEFAULT_INTERVAL_MINUTES = 60

_GRAPH_DATETIME = "%Y-%m-%dT%H:%M:%S"
_UTC_NAMES = frozenset({"UTC", "GMT", "ETC/UTC", "ETC/GMT"})
# Graph writes 7 fractional digits ("08:00:00.0000000"); Python reads 6.
_FRACTION = re.compile(r"(\.\d{6})\d+")

# Graph names a mailbox's zone in Windows form, which ZoneInfo cannot read. An
# id missing here resolves to None, and the caller treats the hours as absent.
WINDOWS_TIME_ZONES: dict[str, str] = {
    "Arabian Standard Time": "Asia/Dubai",
    "Arab Standard Time": "Asia/Riyadh",
    "Arabic Standard Time": "Asia/Baghdad",
    "GMT Standard Time": "Europe/London",
    "Greenwich Standard Time": "Atlantic/Reykjavik",
    "W. Europe Standard Time": "Europe/Berlin",
    "Central Europe Standard Time": "Europe/Budapest",
    "Central European Standard Time": "Europe/Warsaw",
    "Romance Standard Time": "Europe/Paris",
    "E. Europe Standard Time": "Europe/Chisinau",
    "Turkey Standard Time": "Europe/Istanbul",
    "Egypt Standard Time": "Africa/Cairo",
    "E. Africa Standard Time": "Africa/Nairobi",
    "South Africa Standard Time": "Africa/Johannesburg",
    "India Standard Time": "Asia/Kolkata",
    "Pakistan Standard Time": "Asia/Karachi",
    "Iran Standard Time": "Asia/Tehran",
    "Singapore Standard Time": "Asia/Singapore",
    "China Standard Time": "Asia/Shanghai",
    "Tokyo Standard Time": "Asia/Tokyo",
    "AUS Eastern Standard Time": "Australia/Sydney",
    "Eastern Standard Time": "America/New_York",
    "Central Standard Time": "America/Chicago",
    "Mountain Standard Time": "America/Denver",
    "Pacific Standard Time": "America/Los_Angeles",
    "UTC": "UTC",
}

_ISO_DAYS = {
    "monday": 1,
    "tuesday": 2,
    "wednesday": 3,
    "thursday": 4,
    "friday": 5,
    "saturday": 6,
    "sunday": 7,
}


def resolve_time_zone(name: str) -> ZoneInfo | None:
    """An IANA or Windows zone name as a ``ZoneInfo``; ``None`` when neither reads."""
    for candidate in (name, WINDOWS_TIME_ZONES.get(name, "")):
        if not candidate:
            continue
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return None


class FreeBusyStatus(StrEnum):
    """Graph ``scheduleItems[].status``. Only ``FREE`` is bookable."""

    FREE = "free"
    TENTATIVE = "tentative"
    BUSY = "busy"
    OOF = "oof"
    WORKING_ELSEWHERE = "workingElsewhere"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class BusyInterval:
    """One ``scheduleItems`` entry. ``start``/``end`` are aware, in UTC."""

    start: datetime
    end: datetime
    status: FreeBusyStatus

    @property
    def free(self) -> bool:
        return self.status is FreeBusyStatus.FREE


@dataclass(frozen=True)
class WorkingHours:
    """When someone may be booked, in their own zone.

    ``days_of_week`` are Graph's lowercase English names (``"monday"``).
    ``time_zone`` is an IANA or Windows zone name, whichever Graph reported.
    ``end_time`` earlier than ``start_time`` is an overnight shift.
    """

    days_of_week: tuple[str, ...]
    start_time: time
    end_time: time
    time_zone: str

    def zone(self) -> ZoneInfo | None:
        return resolve_time_zone(self.time_zone)

    def iso_days(self) -> frozenset[int]:
        """ISO weekday numbers (1 = Monday) for the names this library knows."""
        return frozenset(
            _ISO_DAYS[d.lower()] for d in self.days_of_week if d.lower() in _ISO_DAYS
        )

    @property
    def usable(self) -> bool:
        """Days that read, a zone that resolves and a non-empty day."""
        return bool(self.iso_days()) and self.zone() is not None and (
            self.start_time != self.end_time
        )


@dataclass(frozen=True)
class MailboxSchedule:
    """One requested address's answer.

    ``readable=False`` means Graph errored on the address, omitted it, or
    withheld the detail; ``intervals`` is then one ``unknown`` interval over
    the whole requested window and ``error`` carries Graph's reason when it
    gave one. ``working_hours`` is ``None`` when the mailbox exposes none that
    read (callers fall back to their own default hours).
    """

    mailbox: str
    readable: bool
    intervals: tuple[BusyInterval, ...]
    working_hours: WorkingHours | None = None
    availability_view: str = ""
    error: str | None = None

    @property
    def busy(self) -> tuple[BusyInterval, ...]:
        """Every interval that blocks a booking (all but ``free``)."""
        return tuple(i for i in self.intervals if not i.free)


class FreeBusyReader:
    """Reads free/busy for a set of mailboxes over Graph ``getSchedule``.

    ``mailbox`` is the user id or UPN the call runs under
    (``/users/{mailbox}/calendar/getSchedule``); app-only there is no ``/me``,
    so any licensed mailbox the application permission covers will do, and
    typically the same service mailbox that organises meetings.

    ``max_per_request`` splits long address lists into batches (at most 100,
    Graph's cap). Batches run one after another, and a whole-request failure
    in any batch raises :class:`FreeBusyError`: the answer is all or nothing.
    """

    def __init__(
        self,
        client: GraphClient,
        mailbox: str,
        *,
        max_per_request: int = MAX_SCHEDULES_PER_REQUEST,
    ) -> None:
        if not mailbox:
            raise GraphConfigError("FreeBusyReader needs a mailbox (user id or UPN)")
        if not 1 <= max_per_request <= MAX_SCHEDULES_PER_REQUEST:
            raise GraphConfigError(
                f"max_per_request must be 1..{MAX_SCHEDULES_PER_REQUEST}, "
                f"got {max_per_request}"
            )
        self._client = client
        self._mailbox = mailbox
        self._max_per_request = max_per_request

    @property
    def mailbox(self) -> str:
        return self._mailbox

    async def get_schedule(
        self,
        mailboxes: Iterable[str],
        start: datetime,
        end: datetime,
        *,
        interval_minutes: int = _DEFAULT_INTERVAL_MINUTES,
    ) -> list[MailboxSchedule]:
        """One :class:`MailboxSchedule` per requested address, in the order
        requested (duplicates included). ``start``/``end`` must be
        timezone-aware; they are sent in UTC. ``interval_minutes`` is Graph's
        ``availabilityViewInterval`` (5..1440); it sizes only the
        ``availability_view`` string, never the intervals."""
        requested = list(mailboxes)
        if any(not m for m in requested):
            raise ValueError("mailboxes must be non-empty addresses")
        start_utc, end_utc = _validated_window(start, end)
        if not _MIN_INTERVAL_MINUTES <= interval_minutes <= _MAX_INTERVAL_MINUTES:
            raise ValueError(
                f"interval_minutes must be {_MIN_INTERVAL_MINUTES}.."
                f"{_MAX_INTERVAL_MINUTES}, got {interval_minutes}"
            )
        if not requested:
            return []

        # One wire entry per address whatever its casing; the answer is still
        # one schedule per requested address, in the caller's order and case.
        unique: dict[str, str] = {}
        for address in requested:
            unique.setdefault(address.lower(), address)
        wire = list(unique.values())

        entries: dict[str, dict[str, Any]] = {}
        for offset in range(0, len(wire), self._max_per_request):
            batch = wire[offset : offset + self._max_per_request]
            entries.update(await self._read(batch, start_utc, end_utc, interval_minutes))

        return [
            _to_schedule(address, entries.get(address.lower()), start_utc, end_utc)
            for address in requested
        ]

    async def _read(
        self, batch: Sequence[str], start: datetime, end: datetime, interval: int
    ) -> dict[str, dict[str, Any]]:
        payload = {
            "schedules": list(batch),
            "startTime": _graph_datetime(start),
            "endTime": _graph_datetime(end),
            "availabilityViewInterval": interval,
        }
        path = f"/users/{self._mailbox}/calendar/getSchedule"
        try:
            body = await self._client.post(path, payload)
        except GraphRequestError as exc:
            raise FreeBusyError(f"Graph returned {exc.status}", detail=exc.detail) from exc
        except httpx.HTTPError as exc:
            # A timeout or dropped connection is a failed read, never an
            # empty calendar. (Clients that already wrap transport failures
            # in GraphRequestError are caught above.)
            raise FreeBusyError(type(exc).__name__) from exc

        values = body.get("value") if isinstance(body, dict) else None
        if not isinstance(values, list):
            raise FreeBusyError("getSchedule answer has no value[]", detail=body)
        # Graph echoes the address as scheduleId, in its own case and order.
        return {
            str(entry["scheduleId"]).lower(): entry
            for entry in values
            if isinstance(entry, dict) and entry.get("scheduleId")
        }


# -- parsing -------------------------------------------------------------------


def _validated_window(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    for name, value in (("start", start), ("end", end)):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(
                f"{name} must be a timezone-aware datetime; a naive one would be "
                f"read as UTC whatever the caller meant"
            )
    start_utc, end_utc = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    if end_utc <= start_utc:
        raise ValueError(
            f"end ({end_utc.isoformat()}) must be after start ({start_utc.isoformat()})"
        )
    return start_utc, end_utc


def _graph_datetime(value_utc: datetime) -> dict[str, str]:
    return {"dateTime": value_utc.strftime(_GRAPH_DATETIME), "timeZone": "UTC"}


def _to_schedule(
    address: str, entry: dict[str, Any] | None, start: datetime, end: datetime
) -> MailboxSchedule:
    items = (entry or {}).get("scheduleItems") or []
    view = str((entry or {}).get("availabilityView") or "")
    if entry is None or entry.get("error") or _detail_withheld(view, items):
        return MailboxSchedule(
            mailbox=address,
            readable=False,
            intervals=(BusyInterval(start, end, FreeBusyStatus.UNKNOWN),),
            availability_view=view,
            error=_error_reason(entry, view),
        )
    return MailboxSchedule(
        mailbox=address,
        readable=True,
        intervals=tuple(_to_interval(item) for item in items),
        working_hours=_to_working_hours(entry.get("workingHours")),
        availability_view=view,
    )


def _error_reason(entry: dict[str, Any] | None, view: str) -> str:
    if entry is None:
        return "not returned by Graph"
    error = entry.get("error")
    if isinstance(error, dict):
        code, message = error.get("responseCode"), error.get("message")
        return ": ".join(str(p) for p in (code, message) if p) or "error"
    if error:
        return str(error)
    return "availability shared without detail"


def _detail_withheld(view: str, items: list[Any]) -> bool:
    """True when ``availabilityView`` marks busy time that ``scheduleItems``
    omits. A mailbox sharing only availability would otherwise read as an
    empty calendar. Graph writes ``workingElsewhere`` as ``0`` here, a known
    blind spot of this tripwire; the intervals stay authoritative."""
    return not items and any(slot not in ("0", " ") for slot in view)


def _to_interval(item: dict[str, Any]) -> BusyInterval:
    raw_status = item.get("status")
    try:
        status = FreeBusyStatus(raw_status)
    except ValueError as exc:
        raise UnmappedFreeBusyStatusError(raw_status) from exc
    start, end = _parse_graph_time(item.get("start")), _parse_graph_time(item.get("end"))
    return BusyInterval(start=start, end=end, status=status)


def _parse_graph_time(raw: Any) -> datetime:
    if not isinstance(raw, dict) or not raw.get("dateTime"):
        raise FreeBusyError("scheduleItem is missing a start or end", detail=raw)
    zone = raw.get("timeZone")
    if zone and str(zone).upper() not in _UTC_NAMES:
        # The wall clock is only meaningful in the zone Graph names it in.
        raise FreeBusyError(f"getSchedule answered in {zone}, not UTC", detail=raw)
    try:
        parsed = datetime.fromisoformat(_FRACTION.sub(r"\1", str(raw["dateTime"])))
    except ValueError as exc:
        raise FreeBusyError("unreadable scheduleItem time", detail=raw) from exc
    # Graph sends the wall clock naive; an offset, if ever present, is kept.
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _to_working_hours(raw: Any) -> WorkingHours | None:
    """Hours without days or without a zone name cannot be applied, and a
    half-read object would hide the caller's fallback, so both read as None.
    Unreadable hours are absent hours, never a failed call."""
    if not isinstance(raw, dict):
        return None
    days = raw.get("daysOfWeek") or []
    zone = (raw.get("timeZone") or {}).get("name") or ""
    if not days or not zone:
        return None
    try:
        return WorkingHours(
            days_of_week=tuple(str(d).lower() for d in days),
            start_time=time.fromisoformat(_FRACTION.sub(r"\1", raw["startTime"])),
            end_time=time.fromisoformat(_FRACTION.sub(r"\1", raw["endTime"])),
            time_zone=zone,
        )
    except (KeyError, TypeError, ValueError):
        return None
