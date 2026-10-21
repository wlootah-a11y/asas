"""FreeBusyReader over a MockTransport: nothing leaves the process."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from datetime import datetime, time, timedelta, timezone

import httpx
import pytest

from asas_graph import (
    BusyInterval,
    FreeBusyError,
    FreeBusyReader,
    FreeBusyStatus,
    GraphClient,
    GraphConfigError,
    GraphError,
    GraphRequestError,
    StaticTokenProvider,
    UnmappedFreeBusyStatusError,
    WorkingHours,
    resolve_time_zone,
)

UAE = timezone(timedelta(hours=4))
START = datetime(2026, 9, 29, 9, 0, tzinfo=UAE)   # 05:00Z
END = datetime(2026, 9, 29, 17, 0, tzinfo=UAE)    # 13:00Z


def _item(status: str, start: str, end: str, zone: str = "UTC") -> dict:
    return {
        "status": status,
        "subject": "never read",
        "start": {"dateTime": start, "timeZone": zone},
        "end": {"dateTime": end, "timeZone": zone},
    }


def _entry(address: str, items=(), view: str = "", hours: dict | None = None) -> dict:
    entry = {"scheduleId": address, "availabilityView": view, "scheduleItems": list(items)}
    if hours is not None:
        entry["workingHours"] = hours
    return entry


class EchoGraph:
    """Answers each getSchedule with one readable, empty entry per address,
    unless ``entries`` scripts one. Records every payload."""

    def __init__(self, entries: dict | None = None, status: int = 200, body=None,
                 headers: dict | None = None, raise_exc: Exception | None = None):
        self.entries = entries or {}
        self.status, self.body, self.headers = status, body, headers or {}
        self.raise_exc = raise_exc
        self.calls: list[tuple[str, dict]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        self.calls.append((str(request.url), payload))
        if self.raise_exc is not None:
            raise self.raise_exc
        if self.body is not None or self.status >= 400:
            return httpx.Response(self.status, json=self.body, headers=self.headers)
        value = []
        for address in payload["schedules"]:
            if address in self.entries:
                if self.entries[address] is not None:  # None = Graph omits it
                    value.append(self.entries[address])
            else:
                value.append(_entry(address, view="0" * 8))
        return httpx.Response(200, json={"value": value})


def _reader(settings, fake: EchoGraph, **kwargs) -> FreeBusyReader:
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    client = GraphClient(settings, token_provider=StaticTokenProvider("t"), http=http)
    return FreeBusyReader(client, "svc@example.gov", **kwargs)


def _run(coro):
    return asyncio.run(coro)


# -- the wire ------------------------------------------------------------------


def test_the_request_is_sent_in_utc_to_the_service_mailbox(settings):
    fake = EchoGraph()
    _run(_reader(settings, fake).get_schedule(["a@x.com"], START, END, interval_minutes=30))
    url, payload = fake.calls[0]
    assert url == "https://graph.microsoft.com/v1.0/users/svc@example.gov/calendar/getSchedule"
    # The UAE +04:00 window goes out as UTC wall clock labelled UTC.
    assert payload == {
        "schedules": ["a@x.com"],
        "startTime": {"dateTime": "2026-09-29T05:00:00", "timeZone": "UTC"},
        "endTime": {"dateTime": "2026-09-29T13:00:00", "timeZone": "UTC"},
        "availabilityViewInterval": 30,
    }


def test_more_than_the_cap_is_split_into_batches_in_order(settings):
    fake = EchoGraph()
    addresses = [f"p{i}@x.com" for i in range(205)]
    out = _run(_reader(settings, fake).get_schedule(addresses, START, END))
    assert [len(p["schedules"]) for _, p in fake.calls] == [100, 100, 5]
    assert [s.mailbox for s in out] == addresses
    assert all(s.readable and s.intervals == () for s in out)


def test_a_smaller_batch_size_is_honoured(settings):
    fake = EchoGraph()
    _run(_reader(settings, fake, max_per_request=2).get_schedule(
        ["a@x.com", "b@x.com", "c@x.com", "d@x.com", "e@x.com"], START, END))
    assert [p["schedules"] for _, p in fake.calls] == [
        ["a@x.com", "b@x.com"], ["c@x.com", "d@x.com"], ["e@x.com"]]


@pytest.mark.parametrize("size", [0, 101])
def test_a_batch_size_outside_graphs_cap_is_a_config_error(settings, size):
    with pytest.raises(GraphConfigError):
        _reader(settings, EchoGraph(), max_per_request=size)


def test_a_reader_needs_a_mailbox(settings, client):
    with pytest.raises(GraphConfigError):
        FreeBusyReader(client, "")


def test_addresses_match_case_insensitively_and_duplicates_go_once(settings):
    fake = EchoGraph({"Chen@X.com": _entry(
        "chen@x.com", [_item("busy", "2026-09-29T06:00:00.0000000", "2026-09-29T07:00:00.0000000")],
        view="01000000")})
    out = _run(_reader(settings, fake).get_schedule(["Chen@X.com", "chen@x.com"], START, END))
    assert fake.calls[0][1]["schedules"] == ["Chen@X.com"]
    assert [s.mailbox for s in out] == ["Chen@X.com", "chen@x.com"]
    assert out[0] == dataclasses.replace(out[1], mailbox="Chen@X.com")
    assert out[0].intervals == (BusyInterval(
        datetime(2026, 9, 29, 6, tzinfo=timezone.utc),
        datetime(2026, 9, 29, 7, tzinfo=timezone.utc), FreeBusyStatus.BUSY),)


def test_no_mailboxes_means_no_call(settings):
    fake = EchoGraph()
    assert _run(_reader(settings, fake).get_schedule([], START, END)) == []
    assert fake.calls == []


@pytest.mark.parametrize("start,end", [
    (datetime(2026, 9, 29, 9), datetime(2026, 9, 29, 17)),  # naive
    (END, START),                                           # inverted
])
def test_a_bad_window_is_refused_before_the_call(settings, start, end):
    fake = EchoGraph()
    with pytest.raises(ValueError):
        _run(_reader(settings, fake).get_schedule(["a@x.com"], start, end))
    assert fake.calls == []


@pytest.mark.parametrize("interval", [4, 1441])
def test_an_interval_outside_graphs_range_is_refused(settings, interval):
    with pytest.raises(ValueError):
        _run(_reader(settings, EchoGraph()).get_schedule(
            ["a@x.com"], START, END, interval_minutes=interval))


# -- per-mailbox answers ---------------------------------------------------------


def test_every_non_free_status_is_busy_and_free_is_not(settings):
    statuses = ["free", "tentative", "busy", "oof", "workingElsewhere", "unknown"]
    items = [_item(s, f"2026-09-29T{5 + i:02d}:00:00", f"2026-09-29T{5 + i:02d}:30:00")
             for i, s in enumerate(statuses)]
    fake = EchoGraph({"a@x.com": _entry("a@x.com", items, view="02121200")})
    (schedule,) = _run(_reader(settings, fake).get_schedule(["a@x.com"], START, END))
    assert [i.status.value for i in schedule.intervals] == statuses
    assert [i.free for i in schedule.intervals] == [True] + [False] * 5
    assert [i.status.value for i in schedule.busy] == statuses[1:]


def test_errors_per_mailbox_are_flagged_and_busy_while_the_rest_read(settings):
    """One bad mailbox never fails the call, and never reads as free."""
    fake = EchoGraph({
        "err@x.com": {"scheduleId": "err@x.com",
                      "error": {"message": "not found", "responseCode": "ErrorMailboxNotFound"}},
        "gone@x.com": None,
        "hidden@x.com": _entry("hidden@x.com", items=(), view="00220000"),
        "entryless@x.com": {"availabilityView": "0"},  # no scheduleId: matches nobody
    })
    addresses = ["ok@x.com", "err@x.com", "gone@x.com", "hidden@x.com", "entryless@x.com"]
    out = _run(_reader(settings, fake).get_schedule(addresses, START, END))
    by = {s.mailbox: s for s in out}
    assert by["ok@x.com"].readable and by["ok@x.com"].error is None
    whole = (BusyInterval(START.astimezone(timezone.utc), END.astimezone(timezone.utc),
                          FreeBusyStatus.UNKNOWN),)
    for address in addresses[1:]:
        assert not by[address].readable, address
        assert by[address].intervals == whole and by[address].busy == whole
    assert by["err@x.com"].error == "ErrorMailboxNotFound: not found"
    assert by["gone@x.com"].error == "not returned by Graph"
    assert by["hidden@x.com"].error == "availability shared without detail"


def test_an_all_free_view_without_items_is_an_empty_readable_calendar(settings):
    fake = EchoGraph({"a@x.com": _entry("a@x.com", view="00000000")})
    (schedule,) = _run(_reader(settings, fake).get_schedule(["a@x.com"], START, END))
    assert schedule.readable and schedule.busy == ()


def test_an_unmapped_status_raises_and_is_not_transient(settings):
    fake = EchoGraph({"a@x.com": _entry("a@x.com", [
        _item("elsewhere-ish", "2026-09-29T05:00:00", "2026-09-29T06:00:00")], view="2")})
    with pytest.raises(UnmappedFreeBusyStatusError) as exc:
        _run(_reader(settings, fake).get_schedule(["a@x.com"], START, END))
    assert exc.value.status == "elsewhere-ish"
    assert isinstance(exc.value, FreeBusyError) and not exc.value.is_transient


def test_a_non_utc_answer_is_refused_not_assumed(settings):
    fake = EchoGraph({"a@x.com": _entry("a@x.com", [
        _item("busy", "2026-09-29T09:00:00", "2026-09-29T10:00:00",
              zone="Arabian Standard Time")], view="2")})
    with pytest.raises(FreeBusyError, match="Arabian Standard Time"):
        _run(_reader(settings, fake).get_schedule(["a@x.com"], START, END))


def test_an_offset_bearing_time_is_normalised_to_utc(settings):
    fake = EchoGraph({"a@x.com": _entry("a@x.com", [
        _item("busy", "2026-09-29T10:00:00+04:00", "2026-09-29T11:00:00+04:00", zone="")],
        view="2")})
    (schedule,) = _run(_reader(settings, fake).get_schedule(["a@x.com"], START, END))
    assert schedule.intervals[0].start == datetime(2026, 9, 29, 6, tzinfo=timezone.utc)
    assert schedule.intervals[0].start.tzinfo is timezone.utc


def test_uae_working_hours_arrive_with_a_windows_zone(settings):
    hours = {"daysOfWeek": ["Monday", "tuesday", "wednesday", "thursday", "friday"],
             "startTime": "08:00:00.0000000", "endTime": "17:00:00.0000000",
             "timeZone": {"name": "Arabian Standard Time"}}
    fake = EchoGraph({"a@x.com": _entry("a@x.com", view="0", hours=hours)})
    (schedule,) = _run(_reader(settings, fake).get_schedule(["a@x.com"], START, END))
    wh = schedule.working_hours
    assert wh == WorkingHours(("monday", "tuesday", "wednesday", "thursday", "friday"),
                              time(8), time(17), "Arabian Standard Time")
    assert wh.zone().key == "Asia/Dubai" and wh.iso_days() == {1, 2, 3, 4, 5} and wh.usable
    assert datetime(2026, 9, 29, 8, tzinfo=wh.zone()).utcoffset() == timedelta(hours=4)


@pytest.mark.parametrize("hours", [
    {"daysOfWeek": [], "startTime": "08:00:00", "endTime": "17:00:00",
     "timeZone": {"name": "UTC"}},
    {"daysOfWeek": ["monday"], "startTime": "08:00:00", "endTime": "17:00:00",
     "timeZone": {}},
    {"daysOfWeek": ["monday"], "startTime": "8 o'clock", "endTime": "17:00:00",
     "timeZone": {"name": "UTC"}},
    {"daysOfWeek": ["monday"], "timeZone": {"name": "UTC"}},
])
def test_half_read_working_hours_are_absent_hours(settings, hours):
    fake = EchoGraph({"a@x.com": _entry("a@x.com", view="0", hours=hours)})
    (schedule,) = _run(_reader(settings, fake).get_schedule(["a@x.com"], START, END))
    assert schedule.readable and schedule.working_hours is None


def test_zone_names_resolve_iana_windows_or_not_at_all():
    assert resolve_time_zone("Asia/Dubai").key == "Asia/Dubai"
    assert resolve_time_zone("Arabian Standard Time").key == "Asia/Dubai"
    assert resolve_time_zone("Customized Time Zone") is None
    assert resolve_time_zone("") is None


# -- whole-request failures -------------------------------------------------------


def test_a_throttled_request_fails_the_read_as_transient(settings):
    fake = EchoGraph(status=429, body={"error": {"code": "TooManyRequests"}},
                     headers={"Retry-After": "7"})
    with pytest.raises(FreeBusyError) as exc:
        _run(_reader(settings, fake).get_schedule(["a@x.com"], START, END))
    assert exc.value.is_transient
    cause = exc.value.__cause__
    assert isinstance(cause, GraphRequestError) and cause.retry_after == 7.0
    assert cause.code == "TooManyRequests"


def test_a_failed_second_batch_fails_the_whole_read(settings):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 2:
            return httpx.Response(400, json={"error": {"code": "ErrorMailboxDataArrayTooBig"}})
        schedules = json.loads(request.content)["schedules"]
        return httpx.Response(200, json={"value": [_entry(a, view="0") for a in schedules]})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    reader = FreeBusyReader(GraphClient(settings, token_provider=StaticTokenProvider("t"),
                                        http=http), "svc@example.gov", max_per_request=1)
    with pytest.raises(FreeBusyError) as exc:
        _run(reader.get_schedule(["a@x.com", "b@x.com", "c@x.com"], START, END))
    assert "ErrorMailboxDataArrayTooBig" in str(exc.value.detail)
    assert not exc.value.is_transient and len(calls) == 2


def test_a_dropped_connection_is_a_transient_read_error(settings):
    fake = EchoGraph(raise_exc=httpx.ConnectError("reset"))
    with pytest.raises(FreeBusyError) as exc:
        _run(_reader(settings, fake).get_schedule(["a@x.com"], START, END))
    assert isinstance(exc.value, GraphError) and exc.value.is_transient


def test_an_answer_without_value_is_a_read_error(settings):
    fake = EchoGraph(body={"unexpected": True})
    with pytest.raises(FreeBusyError, match="value"):
        _run(_reader(settings, fake).get_schedule(["a@x.com"], START, END))
