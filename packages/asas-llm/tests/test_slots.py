"""A cap that holds across every process, with a stand-in for the Redis
sorted set that runs exactly the commands the semaphore sends."""

import asyncio
import threading

import pytest

from asas_llm import slots as mod


class SharedRedis:
    """The server: a sorted set per key and a clock in milliseconds."""

    def __init__(self) -> None:
        self.now_ms = 1_000_000
        self.sets: dict[str, dict[str, int]] = {}
        self.down = False


class FakeClient:
    """One process's connection to the shared server."""

    def __init__(self, server: SharedRedis) -> None:
        self.server = server

    def _check(self) -> None:
        if self.server.down:
            raise ConnectionError("redis is down")

    async def eval(self, script, numkeys, *args):
        self._check()
        assert numkeys == 1
        key, token, *rest = args
        holders = self.server.sets.setdefault(key, {})
        now = self.server.now_ms
        if script == mod.ACQUIRE:
            limit, lease = int(rest[0]), int(rest[1])
            for member, until in list(holders.items()):
                if until <= now:
                    del holders[member]
            if len(holders) < limit:
                holders[token] = now + lease
                return 1
            return 0
        if script == mod.RENEW:
            if token not in holders:
                return 0
            holders[token] = now + int(rest[0])
            return 1
        raise AssertionError("a script the stand-in does not know")

    async def zrem(self, key, *members):
        self._check()
        holders = self.server.sets.setdefault(key, {})
        return sum(1 for m in members if holders.pop(m, None) is not None)


def _process(server, limit=2, lease=15.0, prefix=mod.PREFIX):
    client = FakeClient(server)
    return mod.WideSemaphore("speech", limit, lease_seconds=lease, client=lambda: client, poll_seconds=0.001, prefix=prefix)


async def _work(sem, counter, hold_for=0.01):
    async with sem.hold():
        counter["now"] += 1
        counter["peak"] = max(counter["peak"], counter["now"])
        await asyncio.sleep(hold_for)
        counter["now"] -= 1


@pytest.mark.asyncio
async def test_never_more_than_the_limit_across_processes():
    server = SharedRedis()
    a, b = _process(server), _process(server)
    counter = {"now": 0, "peak": 0}
    await asyncio.gather(*(_work(p, counter) for p in [a, b] * 6))
    assert counter["peak"] == 2
    assert server.sets[mod.PREFIX + "speech"] == {}


@pytest.mark.asyncio
async def test_a_lapsed_lease_frees_its_slot():
    server = SharedRedis()
    key = mod.PREFIX + "speech"
    until = server.now_ms + 15_000
    server.sets[key] = {"dead-1": until, "dead-2": until}
    sem = _process(server)
    waiting = asyncio.create_task(sem._acquire())
    await asyncio.sleep(0.02)
    assert not waiting.done()
    server.now_ms += 15_000
    token = await asyncio.wait_for(waiting, 1)
    assert set(server.sets[key]) == {token}


@pytest.mark.asyncio
async def test_a_slot_is_given_back_when_the_work_raises():
    server = SharedRedis()
    sem = _process(server, limit=1)
    with pytest.raises(RuntimeError):
        async with sem.hold():
            assert len(server.sets[mod.PREFIX + "speech"]) == 1
            raise RuntimeError("recognizer failed")
    assert server.sets[mod.PREFIX + "speech"] == {}
    async with _process(server, limit=1).hold():
        pass


@pytest.mark.asyncio
async def test_a_held_lease_is_renewed_so_it_does_not_lapse():
    server = SharedRedis()
    key = mod.PREFIX + "speech"
    sem = _process(server, limit=1, lease=0.03)
    async with sem.hold():
        (token,) = server.sets[key]
        first = server.sets[key][token]
        server.now_ms += 20
        await asyncio.sleep(0.05)
        assert server.sets[key][token] > first
        server.now_ms += 20
        assert await FakeClient(server).eval(mod.ACQUIRE, 1, key, "x", 1, 30) == 0


@pytest.mark.asyncio
async def test_redis_down_falls_back_to_a_per_process_cap():
    server = SharedRedis()
    server.down = True
    sem = _process(server)
    counter = {"now": 0, "peak": 0}
    await asyncio.gather(*(_work(sem, counter) for _ in range(6)))
    assert counter["peak"] == 2


@pytest.mark.asyncio
async def test_redis_failing_on_release_still_frees_the_local_slot():
    server = SharedRedis()
    sem = _process(server, limit=1)
    async with sem.hold():
        server.down = True
    async with sem.hold():
        pass


def test_each_event_loop_gets_its_own_local_semaphore():
    server = SharedRedis()
    server.down = True
    sem = _process(server, limit=1)
    errors: list[BaseException] = []

    def run() -> None:
        async def contended():
            await asyncio.gather(*(_work(sem, {"now": 0, "peak": 0}) for _ in range(3)))

        try:
            asyncio.run(contended())
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


@pytest.mark.asyncio
async def test_a_host_keeps_its_own_key_prefix():
    server = SharedRedis()
    sem = _process(server, prefix="my-app:")
    async with sem.hold():
        assert set(server.sets) == {"my-app:speech"}
    assert sem.key == "my-app:speech"
