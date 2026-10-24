"""A concurrency cap that holds across every worker and replica.

An ``asyncio.Semaphore`` caps one process. A service that runs several web
workers per replica, several replicas and background workers besides lets N
times that many through, so a limit set to stay under an external service's
own quota (a model gateway, a speech recogniser) is then not a limit at all.

The holders live in a Redis sorted set, each scored by when its lease ends.
Taking a slot is one Lua script: drop the lapsed leases, count the rest, and
add ours if there is room, so two processes can never both see the last slot
free. Time is Redis's own (``TIME``), never a replica's clock. A holder renews
its lease while it holds, so the lease can be short: a process that dies
holding a slot gives it back when the lease runs out, not never.

In front of that sits a per-process semaphore of the same size. It keeps a
process from polling Redis with more waiters than could ever hold a slot,
keeps the waiters in one process in order, and is what remains when Redis
cannot be reached: the cap falls back to per process rather than to none or
to a stall.

The local semaphore is per event loop. An asyncio primitive is bound to the
loop that first waits on it, and a Celery thread pool runs a loop per task
thread, so one module-level semaphore shared across them raises.

``hold(limit=...)`` takes a slot only while fewer than ``limit`` (at most the
semaphore's own) are held. Each way of holding has its own local semaphore,
so a queue of one kind never stands in front of Redis for another.

Priority (``hold(urgent=True)`` / ``hold(give_way=n)``; :mod:`asas_llm.admission`
uses it for its interactive and bulk lanes). A second sorted set beside the
holders, ``<key>:urgent``, lists the urgent callers that are waiting, each
scored by when its entry lapses (a few polls, refreshed on every poll, so a
waiter that died drops out within a second). One script, ``PRIORITY_ACQUIRE``,
does it all atomically:

* an urgent caller takes a slot while fewer than ``limit`` are held; if none
  is free it puts itself on the waiting list (and takes itself off when it
  gets one, or gives up);
* a ``give_way=n`` caller takes a slot only while fewer than
  ``limit - min(urgent waiting, n)`` are held. With nobody waiting that is
  every slot; with someone waiting the next slot that frees is theirs.

Invariants: never more than ``limit`` holders; an urgent caller waits for at
most the first slot to free (plus a poll), never behind a give-way caller that
asked first, as long as no more than ``n`` urgent callers wait at once; a
give-way caller is never held below ``limit - n`` by the waiting list, so it
cannot be starved.

A release wakes this loop's waiters at once rather than at their next poll.

With Redis out of reach every holder in the process (every loop and thread)
shares one ``fallback_limit``, polled from a thread-safe semaphore. That is
the process's share of the engine-wide limit, so N processes without Redis
still stay near the limit rather than at N times it. The outage is logged
once when it starts and once when Redis answers again, not on every hold.

The Redis client is a callable the host supplies (sync or async), returning
any client with ``eval(script, numkeys, *keys_and_args)`` and ``zrem``: the
``redis`` package's asyncio client, or a stand-in in tests. The package does
not import ``redis``.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import random
import threading
import uuid
import weakref
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

log = logging.getLogger(__name__)

#: Default key prefix. A host that moves here from its own copy keeps its
#: keys by passing the prefix it used.
PREFIX = "engine-slots:"

#: KEYS[1] the holders; ARGV token, limit, lease (ms). 1 when the slot is ours.
ACQUIRE = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
if redis.call('ZCARD', KEYS[1]) < tonumber(ARGV[2]) then
  redis.call('ZADD', KEYS[1], now + tonumber(ARGV[3]), ARGV[1])
  redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[3]))
  return 1
end
return 0
"""

#: KEYS[1] the holders, KEYS[2] the urgent waiters; ARGV token, limit, lease
#: (ms), urgent (0/1), give_way, waiter ttl (ms). 1 when the slot is ours.
PRIORITY_ACQUIRE = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now)
local ceiling = tonumber(ARGV[2])
local give_way = tonumber(ARGV[5])
if give_way > 0 then
  local waiting = redis.call('ZCARD', KEYS[2])
  ceiling = ceiling - math.min(waiting, give_way)
end
local urgent = tonumber(ARGV[4]) == 1
if redis.call('ZCARD', KEYS[1]) < ceiling then
  redis.call('ZADD', KEYS[1], now + tonumber(ARGV[3]), ARGV[1])
  redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[3]))
  if urgent then
    redis.call('ZREM', KEYS[2], ARGV[1])
  end
  return 1
end
if urgent then
  redis.call('ZADD', KEYS[2], now + tonumber(ARGV[6]), ARGV[1])
  redis.call('PEXPIRE', KEYS[2], tonumber(ARGV[6]))
end
return 0
"""

#: KEYS[1] the holders; ARGV token, lease (ms). 0 when the lease was already
#: taken away (it lapsed and another holder cleared it): never re-added, since
#: the slot may be someone else's by now.
RENEW = """
if not redis.call('ZSCORE', KEYS[1], ARGV[1]) then
  return 0
end
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
redis.call('ZADD', KEYS[1], 'XX', now + tonumber(ARGV[2]), ARGV[1])
local left = redis.call('PTTL', KEYS[1])
if left < tonumber(ARGV[2]) then
  redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[2]))
end
return 1
"""


class WideSemaphore:
    """At most ``limit`` holders of ``name`` across the whole deployment."""

    def __init__(
        self,
        name: str,
        limit: int,
        *,
        lease_seconds: float,
        client: Callable[[], Any],
        poll_seconds: float = 0.1,
        fallback_limit: int | None = None,
        prefix: str = PREFIX,
    ) -> None:
        self._key = prefix + name
        self._urgent_key = self._key + ":urgent"
        self._limit = max(1, int(limit))
        self._lease_ms = max(1, int(lease_seconds * 1000))
        self._client = client
        self._poll_seconds = poll_seconds
        self._local: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop, dict[tuple[int, bool, int], asyncio.Semaphore]
        ] = weakref.WeakKeyDictionary()
        self._wakeups: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Event] = (
            weakref.WeakKeyDictionary()
        )
        # An urgent waiter's entry lapses after a few missed polls.
        self._waiter_ttl_ms = max(1000, int(poll_seconds * 4000))
        self._local_guard = threading.Lock()
        self._fallback_limit = max(1, int(self._limit if fallback_limit is None else fallback_limit))
        # Process-wide, not per loop: a threading primitive, taken without
        # blocking and polled, so it holds across Celery's task threads.
        self._fallback = threading.BoundedSemaphore(self._fallback_limit)
        self._degraded = False

    @property
    def key(self) -> str:
        return self._key

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def fallback_limit(self) -> int:
        return self._fallback_limit

    def _local_slots(self, limit: int, urgent: bool = False, give_way: int = 0) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        kind = (limit, urgent, give_way)
        with self._local_guard:
            by_kind = self._local.get(loop)
            if by_kind is None:
                by_kind = self._local[loop] = {}
            slots = by_kind.get(kind)
            if slots is None:
                slots = by_kind[kind] = asyncio.Semaphore(limit)
            return slots

    @asynccontextmanager
    async def hold(self, limit: int | None = None, *, urgent: bool = False, give_way: int = 0) -> AsyncIterator[None]:
        """One slot for the block, taken only while fewer than ``limit``
        (default, and at most, the semaphore's own) are held deployment-wide.

        ``urgent``: wait on the list that give-way callers defer to.
        ``give_way``: leave up to that many slots to urgent callers that are
        waiting (none while nobody waits). See the module docstring."""
        limit = self._limit if limit is None else max(1, min(int(limit), self._limit))
        give_way = max(0, min(int(give_way), limit - 1))
        async with self._local_slots(limit, urgent, give_way):
            token = await self._acquire(limit, urgent=urgent, give_way=give_way)
            if token is None:
                # Redis is out of reach: the process's share is all there is.
                async with self._fallback_slot():
                    yield
                return
            renewing = asyncio.create_task(self._keep(token))
            try:
                yield
            finally:
                renewing.cancel()
                await self._release(token)
                self._wake()

    def _wake(self) -> None:
        """Wake this loop's waiters: a slot was just given back."""
        loop = asyncio.get_running_loop()
        with self._local_guard:
            event = self._wakeups.pop(loop, None)
        if event is not None:
            event.set()

    async def _pause(self) -> None:
        """Until the next poll, or until this loop gives a slot back."""
        loop = asyncio.get_running_loop()
        with self._local_guard:
            event = self._wakeups.get(loop)
            if event is None:
                event = self._wakeups[loop] = asyncio.Event()
        # Jittered, so the waiters of several processes do not ask in step.
        try:
            await asyncio.wait_for(event.wait(), self._poll_seconds * (0.5 + random.random()))
        except asyncio.TimeoutError:
            pass

    @asynccontextmanager
    async def _fallback_slot(self) -> AsyncIterator[None]:
        while not self._fallback.acquire(blocking=False):
            await asyncio.sleep(self._poll_seconds * (0.5 + random.random()))
        try:
            yield
        finally:
            self._fallback.release()

    def _set_degraded(self, value: bool) -> bool:
        """Whether this call changed it: one log line per outage, not per
        holder, from whichever thread sees the change first."""
        with self._local_guard:
            changed = self._degraded is not value
            self._degraded = value
            return changed

    async def _redis(self) -> Any:
        client = self._client()
        return await client if inspect.isawaitable(client) else client

    async def _acquire(self, limit: int | None = None, *, urgent: bool = False, give_way: int = 0) -> str | None:
        """Our token once a slot is ours, or None when Redis cannot say."""
        limit = self._limit if limit is None else limit
        token = uuid.uuid4().hex
        listed = False
        try:
            while True:
                got = await self._try(token, limit, urgent, give_way)
                if got is None:
                    return None
                if got:
                    listed = False
                    return token
                listed = urgent
                await self._pause()
        finally:
            if listed:
                # Gave up while on the waiting list (cancelled): off it now,
                # not when the entry lapses, so give-way callers resume.
                try:
                    await (await self._redis()).zrem(self._urgent_key, token)
                except Exception:  # noqa: BLE001 -- the entry lapses on its own
                    pass

    async def _try(self, token: str, limit: int, urgent: bool, give_way: int) -> bool | None:
        """One attempt: True when the slot is ours, None when Redis cannot say."""
        try:
            redis = await self._redis()
            if urgent or give_way:
                got = await redis.eval(
                    PRIORITY_ACQUIRE,
                    2,
                    self._key,
                    self._urgent_key,
                    token,
                    limit,
                    self._lease_ms,
                    1 if urgent else 0,
                    give_way,
                    self._waiter_ttl_ms,
                )
            else:
                got = await redis.eval(ACQUIRE, 1, self._key, token, limit, self._lease_ms)
        except Exception as exc:  # noqa: BLE001 -- unreachable reads as "local only"
            if self._set_degraded(True):
                log.warning(
                    "slots %s: Redis unavailable (%s); capping at %d per process until it answers again",
                    self._key,
                    type(exc).__name__,
                    self._fallback_limit,
                )
            return None
        if self._degraded and self._set_degraded(False):
            log.warning("slots %s: Redis is back; capping deployment-wide", self._key)
        return int(got or 0) == 1

    async def _keep(self, token: str) -> None:
        """Renew the lease at a third of its length for as long as it is held."""
        every = self._lease_ms / 3000
        while True:
            await asyncio.sleep(every)
            try:
                kept = await (await self._redis()).eval(RENEW, 1, self._key, token, self._lease_ms)
            except Exception as exc:  # noqa: BLE001 -- the next renewal may land
                log.info("slots %s: could not renew (%s)", self._key, type(exc).__name__)
                continue
            if not int(kept or 0):
                log.warning(
                    "slots %s: a lease lapsed while held; the cap may be exceeded until this holder finishes",
                    self._key,
                )
                return

    async def _release(self, token: str) -> None:
        try:
            await (await self._redis()).zrem(self._key, token)
        except Exception as exc:  # noqa: BLE001 -- the lease lapses on its own
            log.info("slots %s: could not release (%s); the lease will lapse", self._key, type(exc).__name__)


__all__ = ["ACQUIRE", "PREFIX", "PRIORITY_ACQUIRE", "RENEW", "WideSemaphore"]
