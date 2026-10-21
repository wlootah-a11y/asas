"""SlotFinder: pure interval arithmetic, so no transport except the end-to-end case."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, time, timedelta, timezone

import httpx
import pytest

from asas_graph import (
    BusyInterval,
    FreeBusyReader,
    FreeBusyStatus,
    GraphClient,
    GraphConfigError,
    MailboxSchedule,
    Slot,
    SlotFinder,
    StaticTokenProvider,
    WorkingHours,
)

UTC = timezone.utc
UAE = timezone(timedelta(hours=4))
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday")
DUBAI_HOURS = WorkingHours(WEEKDAYS, time(8), time(17), "Arabian Standard Time")
LONDON_HOURS = WorkingHours(WEEKDAYS, time(9), time(17, 30), "GMT Standard Time")
HOUR = timedelta(hours=1)


def z(h: int, m: int = 0, day: int = 29) -> datetime:
    return datetime(2026, 9, day, h, m, tzinfo=UTC)


def busy(start, end, status=FreeBusyStatus.BUSY) -> BusyInterval:
    return BusyInterval(start, end, status)


def sched(mailbox, *intervals, hours=None, readable=True) -> MailboxSchedule:
    return MailboxSchedule(mailbox, readable, tuple(intervals), working_hours=hours)


def test_common_slots_are_exact_and_deterministic():
    schedules = [
        sched("a@x.com", busy(z(9), z(10)), busy(z(12), z(12, 30))),
        sched("b@x.com", busy(z(9, 30), z(11)),
              busy(z(14), z(15), FreeBusyStatus.FREE)),  # free never blocks
    ]
    finder = SlotFinder(step=timedelta(minutes=30))
    first = finder.find(schedules, z(9), z(15), HOUR)
    assert [(s.start, s.end) for s in first] == [
        (z(11), z(12)), (z(12, 30), z(13, 30)), (z(13), z(14)),
        (z(13, 30), z(14, 30)), (z(14), z(15)),
    ]
    # Identical input, identical output, in either schedule order.
    assert finder.find(list(reversed(schedules)), z(9), z(15), HOUR) == first
    assert all(s.start.tzinfo is UTC for s in first)


def test_non_overlapping_and_limit_give_distinct_choices():
    finder = SlotFinder(step=timedelta(minutes=15))
    everyone_free = [sched("a@x.com")]
    assert finder.find(everyone_free, z(9), z(13), HOUR, limit=3, non_overlapping=True) == [
        Slot(z(9), z(10)), Slot(z(10), z(11)), Slot(z(11), z(12))]
    assert [s.start for s in finder.find(everyone_free, z(9), z(13), HOUR, limit=3)] == [
        z(9), z(9, 15), z(9, 30)]


@pytest.mark.parametrize("status", [
    FreeBusyStatus.TENTATIVE, FreeBusyStatus.OOF, FreeBusyStatus.WORKING_ELSEWHERE,
    FreeBusyStatus.UNKNOWN,
])
def test_every_non_free_status_blocks(status):
    slots = SlotFinder(step=HOUR).find([sched("a@x.com", busy(z(9), z(10), status))],
                                       z(9), z(11), HOUR)
    assert slots == [Slot(z(10), z(11))]


def test_an_unreadable_mailbox_blocks_the_whole_window():
    window = busy(z(5), z(13), FreeBusyStatus.UNKNOWN)
    schedules = [sched("ok@x.com"), sched("err@x.com", window, readable=False)]
    assert SlotFinder().find(schedules, z(5), z(13), HOUR) == []


def test_uae_and_london_hours_intersect_in_each_ones_own_zone():
    """Dubai 08:00-17:00 (+04:00) is 04:00-13:00Z; London 09:00-17:30 in
    September is BST (+01:00), 08:00-16:30Z. Common: 08:00-13:00Z."""
    schedules = [sched("dxb@x.com", hours=DUBAI_HOURS),
                 sched("lon@x.com", hours=LONDON_HOURS)]
    slots = SlotFinder(step=HOUR).find(schedules, z(0), z(23), HOUR)
    assert [s.start for s in slots] == [z(8), z(9), z(10), z(11), z(12)]


def test_a_uae_window_is_gridded_on_uae_wall_clock():
    start = datetime(2026, 9, 29, 9, 7, tzinfo=UAE)            # 05:07Z
    end = datetime(2026, 9, 29, 11, 0, tzinfo=UAE)
    slots = SlotFinder(step=timedelta(minutes=15)).find(
        [sched("dxb@x.com", hours=DUBAI_HOURS)], start, end, timedelta(minutes=30),
        limit=2)
    assert [s.start.astimezone(UAE).time() for s in slots] == [time(9, 15), time(9, 30)]
    assert slots[0].start == z(5, 15)


def test_the_uae_weekend_is_outside_hours():
    # 2026-10-03 is a Saturday: nothing inside Dubai hours that day.
    slots = SlotFinder(step=HOUR).find([sched("dxb@x.com", hours=DUBAI_HOURS)],
                                       datetime(2026, 10, 3, tzinfo=UTC),
                                       datetime(2026, 10, 3, 23, tzinfo=UTC), HOUR)
    assert slots == []
    # The same finder does book the Thursday before it.
    assert SlotFinder(step=HOUR).find([sched("dxb@x.com", hours=DUBAI_HOURS)],
                                      datetime(2026, 10, 1, tzinfo=UTC),
                                      datetime(2026, 10, 1, 23, tzinfo=UTC), HOUR)


def test_default_hours_stand_in_for_absent_or_unusable_ones():
    unresolvable = WorkingHours(WEEKDAYS, time(0), time(23), "Customized Time Zone")
    finder = SlotFinder(step=HOUR, default_working_hours=DUBAI_HOURS)
    for hours in (None, unresolvable):
        slots = finder.find([sched("a@x.com", hours=hours)], z(0), z(23), HOUR)
        assert [s.start for s in slots][:1] == [z(4)] and slots[-1].start == z(12)


def test_use_mailbox_hours_false_applies_the_default_to_everyone():
    finder = SlotFinder(step=HOUR, default_working_hours=DUBAI_HOURS, use_mailbox_hours=False)
    slots = finder.find([sched("lon@x.com", hours=LONDON_HOURS)], z(0), z(23), HOUR)
    assert slots[0].start == z(4)


def test_without_any_hours_only_the_window_bounds():
    slots = SlotFinder(step=HOUR).find([sched("a@x.com")], z(22), z(3, day=30), HOUR)
    assert [s.start for s in slots] == [z(22), z(23), z(0, day=30), z(1, day=30), z(2, day=30)]


def test_an_overnight_shift_that_began_yesterday_still_counts():
    night = WorkingHours(WEEKDAYS, time(22), time(6), "UTC")
    slots = SlotFinder(step=HOUR).find([sched("n@x.com", hours=night)],
                                       z(0), z(8), HOUR)
    # Monday 22:00 to Tuesday 06:00 covers Tuesday 00:00-06:00.
    assert [s.start for s in slots] == [z(0), z(1), z(2), z(3), z(4), z(5)]


def test_input_is_validated():
    finder = SlotFinder()
    with pytest.raises(ValueError):
        finder.find([], datetime(2026, 9, 29, 9), z(10), HOUR)
    with pytest.raises(ValueError):
        finder.find([], z(10), z(9), HOUR)
    with pytest.raises(ValueError):
        finder.find([], z(9), z(10), timedelta())
    with pytest.raises(ValueError):
        finder.find([], z(9), z(10), HOUR, limit=0)
    with pytest.raises(GraphConfigError):
        SlotFinder(step=timedelta())
    with pytest.raises(GraphConfigError):
        SlotFinder(default_working_hours=WorkingHours((), time(9), time(17), "UTC"))


def test_end_to_end_from_getschedule_to_slots(settings):
    """A UAE panel read over a MockTransport, then intersected."""
    def handler(request: httpx.Request) -> httpx.Response:
        schedules = json.loads(request.content)["schedules"]
        hours = {"daysOfWeek": list(WEEKDAYS), "startTime": "08:00:00.0000000",
                 "endTime": "17:00:00.0000000", "timeZone": {"name": "Arabian Standard Time"}}
        value = [
            {"scheduleId": schedules[0].upper(), "availabilityView": "0220",
             "workingHours": hours, "scheduleItems": [{
                 "status": "busy", "start": {"dateTime": "2026-09-29T05:00:00.0000000",
                                             "timeZone": "UTC"},
                 "end": {"dateTime": "2026-09-29T07:00:00.0000000", "timeZone": "UTC"}}]},
            {"scheduleId": schedules[1], "availabilityView": "0000",
             "workingHours": hours, "scheduleItems": []},
        ]
        return httpx.Response(200, json={"value": value})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    reader = FreeBusyReader(GraphClient(settings, token_provider=StaticTokenProvider("t"),
                                        http=http), "svc@example.gov")
    start = datetime(2026, 9, 29, 8, tzinfo=UAE)
    end = datetime(2026, 9, 29, 13, tzinfo=UAE)
    schedules = asyncio.run(reader.get_schedule(["a@x.com", "b@x.com"], start, end))
    slots = SlotFinder(step=timedelta(minutes=30)).find(
        schedules, start, end, HOUR, non_overlapping=True)
    # a@ is busy 09:00-11:00 Dubai; the morning before it is a single hour.
    assert [s.start.astimezone(UAE).time() for s in slots] == [time(8), time(11), time(12)]
