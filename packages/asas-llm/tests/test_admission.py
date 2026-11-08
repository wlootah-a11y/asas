"""Model requests at the gateway, counted across processes, with an
interactive lane that bulk gives way to. An in-memory stand-in for the Redis
sorted sets, with a clock the test moves."""

from __future__ import annotations

import asyncio
import logging
import threading

import pytest

from asas_llm import admission as mod
from asas_llm import slots
from asas_llm.admission import Admission


class SharedRedis:
    def __init__(self) -> None:
        self.now_ms = 1_000_000
        self.holders: dict[str, int] = {}
        self.urgent: dict[str, int] = {}
        self.down = False


class FakeClient:
    def __init__(self, server: SharedRedis) -> None:
        self.server = server

    def _check(self) -> None:
        if self.server.down:
            raise ConnectionError("redis is down")

    async def eval(self, script, numkeys, *args):
        self._check()
        holders, now = self.server.holders, self.server.now_ms
        if script == slots.PRIORITY_ACQUIRE:
            assert numkeys == 2
            _, _, token, limit, lease, urgent, give_way, ttl = args
            waiting = self.server.urgent
            for table in (holders, waiting):
                for member, until in list(table.items()):
                    if until <= now:
                        del table[member]
            ceiling = int(limit) - min(len(waiting), int(give_way))
            if len(holders) < ceiling:
                holders[token] = now + int(lease)
                if int(urgent):
                    waiting.pop(token, None)
                return 1
            if int(urgent):
                waiting[token] = now + int(ttl)
            return 0
        assert numkeys == 1
        key, token, *rest = args
        if script == slots.ACQUIRE:
            limit, lease = int(rest[0]), int(rest[1])
            for member, until in list(holders.items()):
                if until <= now:
                    del holders[member]
            if len(holders) < limit:
                holders[token] = now + lease
                return 1
            return 0
        if script == slots.RENEW:
            if token not in holders:
                return 0
            holders[token] = now + int(rest[0])
            return 1
        raise AssertionError("a script the stand-in does not know")

    async def zrem(self, key, *members):
        self._check()
        table = self.server.urgent if key.endswith(":urgent") else self.server.holders
        return sum(1 for m in members if table.pop(m, None) is not None)


class Settings:
    """A host's live settings: the admission reads them on every call."""

    def __init__(self) -> None:
        self.limit, self.reserve, self.processes, self.provider = 4, 1, 2, "core42"


@pytest.fixture
def world():
    server = SharedRedis()
    settings = Settings()
    client = FakeClient(server)
    adm = Admission(
        limit=lambda: settings.limit,
        interactive_reserve=lambda: settings.reserve,
        processes=lambda: settings.processes,
        client=lambda: client,
        shares_quota=lambda provider: provider == settings.provider,
    )
    return server, settings, adm


def _other_process(server: SharedRedis) -> slots.WideSemaphore:
    client = FakeClient(server)
    return slots.WideSemaphore("llm-gateway", 4, lease_seconds=30, client=lambda: client, poll_seconds=0.001)


class Peak:
    def __init__(self) -> None:
        self.now = self.peak = 0

    async def work(self, held, hold_for: float = 0.01) -> None:
        async with held:
            self.now += 1
            self.peak = max(self.peak, self.now)
            await asyncio.sleep(hold_for)
            self.now -= 1


async def _in_lane(adm: Admission, peak: Peak, which: mod.Lane, hold_for: float = 0.01) -> None:
    with mod.lane(which):
        await peak.work(adm.slot("t"), hold_for)


@pytest.mark.asyncio
async def test_the_limit_holds_across_two_processes(world):
    server, _, adm = world
    peak = Peak()
    other = _other_process(server)
    await asyncio.gather(
        *(_in_lane(adm, peak, "interactive") for _ in range(12)),
        *(peak.work(other.hold()) for _ in range(12)),
    )
    assert peak.peak == 4
    assert server.holders == {}


@pytest.mark.asyncio
async def test_bulk_borrows_the_idle_reserve(world):
    server, _, adm = world
    peak = Peak()
    await asyncio.gather(*(_in_lane(adm, peak, "bulk") for _ in range(20)))
    assert peak.peak == 4
    assert server.holders == {} and server.urgent == {}


async def _admitted(adm, order: list, who: str, which: mod.Lane, hold_for: float) -> None:
    with mod.lane(which):
        async with adm.slot("t"):
            order.append(who)
            await asyncio.sleep(hold_for)


@pytest.mark.asyncio
async def test_a_waiting_interactive_call_gets_the_next_free_slot(world):
    server, _, adm = world
    order: list[str] = []
    flood = [asyncio.create_task(_admitted(adm, order, f"bulk-{n}", "bulk", 0.2 + 0.05 * (n % 4))) for n in range(12)]
    await asyncio.sleep(0.05)
    assert len(order) == 4
    loop = asyncio.get_running_loop()
    started = loop.time()
    await _admitted(adm, order, "search", "interactive", 0)
    assert order.index("search") == 4
    assert loop.time() - started < 0.3
    await asyncio.gather(*flood)
    assert server.holders == {} and server.urgent == {}


@pytest.mark.asyncio
async def test_priority_never_exceeds_the_limit_across_two_processes(world):
    server, _, adm = world
    peak = Peak()
    other = _other_process(server)
    await asyncio.gather(
        *(_in_lane(adm, peak, "bulk", 0.005) for _ in range(15)),
        *(_in_lane(adm, peak, "interactive", 0.005) for _ in range(15)),
        *(peak.work(other.hold(give_way=1), 0.005) for _ in range(15)),
        *(peak.work(other.hold(urgent=True), 0.005) for _ in range(15)),
    )
    assert peak.peak == 4
    assert server.holders == {} and server.urgent == {}


@pytest.mark.asyncio
async def test_bulk_keeps_its_old_ceiling_while_someone_waits(world):
    server, _, adm = world
    server.urgent["elsewhere"] = server.now_ms + 1000
    peak = Peak()
    flood = [asyncio.create_task(_in_lane(adm, peak, "bulk", hold_for=0.1)) for _ in range(8)]
    await asyncio.sleep(0.05)
    assert peak.now == 3
    server.now_ms += 1000
    await asyncio.gather(*flood)
    assert peak.peak == 4


@pytest.mark.asyncio
async def test_a_cancelled_interactive_waiter_leaves_the_list(world):
    server, _, adm = world
    flood = [asyncio.create_task(_in_lane(adm, Peak(), "bulk", hold_for=0.3)) for _ in range(4)]
    await asyncio.sleep(0.02)
    waiting = asyncio.create_task(_in_lane(adm, Peak(), "interactive"))
    await asyncio.sleep(0.05)
    assert len(server.urgent) == 1
    waiting.cancel()
    await asyncio.gather(waiting, return_exceptions=True)
    assert server.urgent == {}
    await asyncio.gather(*flood)


@pytest.mark.asyncio
async def test_the_lane_is_inherited_by_the_tasks_a_block_starts():
    seen = []

    async def child():
        seen.append(mod.current_lane())

    with mod.lane("bulk"):
        await asyncio.gather(child(), asyncio.create_task(child()))
    seen.append(mod.current_lane())
    assert seen == ["bulk", "bulk", "interactive"]


@pytest.mark.asyncio
async def test_a_dead_processes_slots_come_back_when_their_leases_lapse(world):
    server, _, adm = world
    lease_ms = int(mod.LEASE_SECONDS * 1000)
    for n in range(4):
        server.holders[f"dead-{n}"] = server.now_ms + lease_ms
    waiting = asyncio.create_task(_in_lane(adm, Peak(), "interactive"))
    await asyncio.sleep(0.1)
    assert not waiting.done()
    server.now_ms += lease_ms
    await asyncio.wait_for(waiting, 1)
    assert server.holders == {}


@pytest.mark.asyncio
async def test_a_failed_call_gives_its_slot_back(world):
    server, _, adm = world
    with pytest.raises(RuntimeError):
        async with adm.slot("t"):
            raise RuntimeError("model said no")
    assert server.holders == {}


@pytest.mark.asyncio
async def test_off_at_zero_never_touches_redis(world):
    server, settings, adm = world
    server.down = True
    settings.limit = 0
    assert not adm.enabled()
    peak = Peak()
    await asyncio.gather(*(_in_lane(adm, peak, "interactive") for _ in range(10)))
    assert peak.peak == 10


@pytest.mark.parametrize("which", ["interactive", "bulk"])
def test_redis_down_falls_back_to_the_process_share_across_threads(world, caplog, which):
    """A worker pool runs a loop per task thread: the fallback is per
    PROCESS (4 // 2 = 2 here), not per loop, and the outage is logged once."""
    server, _, adm = world
    server.down = True
    peak = Peak()
    guard = threading.Lock()
    errors: list[BaseException] = []

    async def one():
        with mod.lane(which):
            async with adm.slot("t"):
                with guard:
                    peak.now += 1
                    peak.peak = max(peak.peak, peak.now)
                await asyncio.sleep(0.02)
                with guard:
                    peak.now -= 1

    def thread():
        async def many():
            await asyncio.gather(*(one() for _ in range(4)))

        try:
            asyncio.run(many())
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    with caplog.at_level(logging.WARNING):
        threads = [threading.Thread(target=thread) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert errors == []
    assert peak.peak == 2
    assert len([r for r in caplog.records if "Redis unavailable" in r.getMessage()]) == 1


@pytest.mark.asyncio
async def test_after_an_outage_the_limit_is_deployment_wide_again(world, caplog):
    server, _, adm = world
    server.down = True
    async with adm.slot("t"):
        pass
    server.down = False
    with caplog.at_level(logging.WARNING):
        peak = Peak()
        other = _other_process(server)
        await asyncio.gather(
            *(_in_lane(adm, peak, "interactive") for _ in range(8)),
            *(peak.work(other.hold()) for _ in range(8)),
        )
    assert peak.peak == 4
    assert any("Redis is back" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_client_off_the_chat_gateway_is_not_admitted(world):
    server, settings, adm = world
    assert adm.shares_quota("core42") and not adm.shares_quota("azure")
    server.down = True  # would be touched if admitted
    peak = Peak()
    await asyncio.gather(*(peak.work(adm.slot("transcribe", provider="azure")) for _ in range(6)))
    assert peak.peak == 6


@pytest.mark.asyncio
async def test_a_changed_limit_rebuilds_the_semaphore(world):
    server, settings, adm = world
    first = adm.semaphore()
    assert first.limit == 4 and first.fallback_limit == 2
    settings.limit = 6
    assert adm.semaphore() is not first and adm.semaphore().limit == 6
    settings.limit = 6
    assert adm.semaphore() is adm.semaphore()
