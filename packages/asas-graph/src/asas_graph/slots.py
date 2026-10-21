"""Common free slots from free/busy, deterministically.

Exact interval arithmetic over aware datetimes: no model call, no clock read,
no randomness, so identical input yields identical output. The candidates are
every step-aligned slot of the given duration inside the window during which
*nobody* passed in is blocked. A mailbox is blocked by

- any interval that is not ``free`` (``tentative`` and ``unknown`` included;
  an unreadable schedule carries one ``unknown`` interval over its whole
  window, so it blocks everything rather than looking empty), and
- time outside its working hours, read in *its own* zone, so a window can
  straddle two of that person's local days. Its own hours apply where Graph
  exposed usable ones, else the finder's ``default_working_hours``, else
  none (the window alone bounds it).

The grid is anchored to local midnight of ``start`` in ``start``'s own zone, so
a 15-minute step in Dubai lands on :00/:15/:30/:45 Dubai time. Output is sorted
by start, in UTC.

What this does **not** do is rank: which of the free slots a product should
offer first (preferred windows, fragmentation of someone's diary, optional
attendees who are busy, minimum notice, holidays) is product policy and stays
in the host.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .errors import GraphConfigError
from .free_busy import MailboxSchedule, WorkingHours

_Span = tuple[datetime, datetime]


@dataclass(frozen=True)
class Slot:
    """A bookable window, in UTC."""

    start: datetime
    end: datetime


class SlotFinder:
    """Finds step-aligned common free slots. Build one per policy and reuse it.

    ``step`` is the grid (default 15 minutes). ``default_working_hours`` stands
    in for any mailbox whose own hours are absent or unusable (no days that
    read, a zone that does not resolve, or start equal to end).
    ``use_mailbox_hours=False`` ignores the mailboxes' own hours and applies
    the default to everyone.
    """

    def __init__(
        self,
        *,
        step: timedelta = timedelta(minutes=15),
        default_working_hours: WorkingHours | None = None,
        use_mailbox_hours: bool = True,
    ) -> None:
        if step <= timedelta():
            raise GraphConfigError(f"step must be positive, got {step}")
        if default_working_hours is not None and not default_working_hours.usable:
            raise GraphConfigError(
                "default_working_hours must have days, a resolvable zone and "
                f"a non-empty day, got {default_working_hours!r}"
            )
        self._step = step
        self._default = default_working_hours
        self._use_own = use_mailbox_hours

    def find(
        self,
        schedules: Iterable[MailboxSchedule],
        start: datetime,
        end: datetime,
        duration: timedelta,
        *,
        limit: int | None = None,
        non_overlapping: bool = False,
    ) -> list[Slot]:
        """Every slot of ``duration`` inside ``[start, end)`` that all
        ``schedules`` are free for, earliest first. ``limit`` caps the count;
        ``non_overlapping=True`` drops a slot that overlaps one already
        returned (so ``limit=4`` yields four distinct choices, not one free
        block offered four times at neighbouring grid positions)."""
        for name, value in (("start", start), ("end", end)):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must be a timezone-aware datetime")
        if duration <= timedelta():
            raise ValueError(f"duration must be positive, got {duration}")
        if limit is not None and limit < 1:
            raise ValueError(f"limit must be at least 1, got {limit}")
        start_utc, end_utc = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
        if end_utc <= start_utc:
            raise ValueError("end must be after start")

        window = (start_utc, end_utc)
        blocked: list[_Span] = []
        for schedule in schedules:
            blocked.extend(
                (i.start.astimezone(timezone.utc), i.end.astimezone(timezone.utc))
                for i in schedule.busy
            )
            hours = self._hours_for(schedule)
            if hours is not None:
                blocked.extend(_off_hours(window, hours))
        merged = _merged(blocked)

        slots: list[Slot] = []
        cursor = 0
        last_end: datetime | None = None
        candidate = _first_on_grid(start, start_utc, self._step)
        while candidate + duration <= end_utc:
            if limit is not None and len(slots) == limit:
                break
            slot_end = candidate + duration
            # Candidates only move forward, so blocks ending at or before this
            # one's start can never overlap a later one either.
            while cursor < len(merged) and merged[cursor][1] <= candidate:
                cursor += 1
            clear = cursor == len(merged) or merged[cursor][0] >= slot_end
            if clear and not (non_overlapping and last_end is not None and candidate < last_end):
                slots.append(Slot(candidate, slot_end))
                last_end = slot_end
            candidate += self._step
        return slots

    def _hours_for(self, schedule: MailboxSchedule) -> WorkingHours | None:
        own = schedule.working_hours
        if self._use_own and own is not None and own.usable:
            return own
        return self._default


def _first_on_grid(start: datetime, start_utc: datetime, step: timedelta) -> datetime:
    """The first grid point at or after ``start``, the grid anchored to local
    midnight of ``start`` in its own zone and walked in absolute time."""
    anchor = start.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(
        timezone.utc
    )
    steps = -(-(start_utc - anchor) // step)  # ceiling division on timedeltas
    return anchor + steps * step


def _off_hours(window: _Span, hours: WorkingHours) -> list[_Span]:
    """The parts of ``window`` outside ``hours``, read in the hours' own zone."""
    tz = hours.zone()
    assert tz is not None  # guarded by WorkingHours.usable
    days = hours.iso_days()
    window_start, window_end = window
    allowed: list[_Span] = []
    # Start a day early: an overnight shift that began yesterday still covers
    # part of this window.
    day = window_start.astimezone(tz).date() - timedelta(days=1)
    last = window_end.astimezone(tz).date()
    while day <= last:
        if day.isoweekday() in days:
            opens = datetime.combine(day, hours.start_time, tz)
            close_day = day + timedelta(days=1) if hours.end_time < hours.start_time else day
            closes = datetime.combine(close_day, hours.end_time, tz)
            lo = max(opens.astimezone(timezone.utc), window_start)
            hi = min(closes.astimezone(timezone.utc), window_end)
            if lo < hi:
                allowed.append((lo, hi))
        day += timedelta(days=1)

    blocks: list[_Span] = []
    cursor = window_start
    for opens, closes in sorted(allowed):
        if cursor < opens:
            blocks.append((cursor, opens))
        cursor = max(cursor, closes)
    if cursor < window_end:
        blocks.append((cursor, window_end))
    return blocks


def _merged(intervals: list[_Span]) -> list[_Span]:
    """Overlapping or touching intervals collapsed, sorted by start."""
    merged: list[_Span] = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged
