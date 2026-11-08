"""How many model requests the whole deployment has in flight, across processes.

A model gateway answers 429 once a key has more requests open than its quota
allows (about six heavy ones on the gateway this came from). A bound that is
per event loop, or none at all, lets four web workers and four worker threads
per replica, times the replicas, open many times that between them, and the
gateway then refuses most of them. Measured on a gateway stub capped at 6 with
4 workers: 39 of 40 CV parses came back with sections missing.

One counter for every process: :class:`~asas_llm.slots.WideSemaphore`, a Redis
sorted set of leases. A holder renews its lease while its request runs, so a
process killed mid-call frees its slot within the lease instead of holding it
forever. A call waits for a slot before it is sent, so the gateway sees at
most ``limit`` requests from the deployment and the waiting happens here
instead of as 429s.

Where a slot is taken: the guarded chat client (:func:`asas_llm.guard.guarded`
with ``admission=admission.slot``), around the request alone; plus any client
that does not go through it but spends the same key's quota (an audio
transcription client, an embeddings client), through ``slot(name,
provider=...)`` and the host's ``shares_quota`` rule.

Two lanes. ``bulk`` work (an intake, a scoring batch) and everything else,
``interactive`` (a person waiting on an answer). Bulk is chosen at the call
site (``with admission.lane("bulk")``) and inherited by every task the block
starts (a ``ContextVar``). Priority, not a static split: bulk may use every
slot while no interactive call is waiting; an interactive call that finds
none free puts itself on a waiting list in Redis, and bulk then sees up to
``interactive_reserve`` of the slots as taken, so the next slot to free goes
to the interactive call. Invariants: never more than ``limit`` in flight; an
interactive call waits at most until the first slot frees, never behind bulk
that queued first; bulk is never held below ``limit - interactive_reserve``,
so it cannot be starved. ``interactive_reserve = 0`` turns priority off.

Off when ``limit`` is 0. When Redis cannot be reached the process falls back
to its share of the limit (``limit // processes``, at least 1) rather than
failing the call or running unbounded: admission is a courtesy to the
gateway, never a reason to drop work.

Every setting may be a value or a zero-argument callable returning one.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from typing import Any, Literal

from .slots import PREFIX, WideSemaphore

log = logging.getLogger(__name__)

Lane = Literal["interactive", "bulk"]
Setting = int | Callable[[], Any]

#: How long a slot stays taken after its holder died. A live holder renews
#: every third of it, so this does not bound how long a request may run.
LEASE_SECONDS = 30.0

_lane: ContextVar[Lane] = ContextVar("asas_llm_admission_lane", default="interactive")


@contextmanager
def lane(name: Lane) -> Iterator[None]:
    """Run the block's model calls in ``name``'s lane."""
    token = _lane.set(name)
    try:
        yield
    finally:
        _lane.reset(token)


def current_lane() -> Lane:
    return _lane.get()


def _read(value: Setting) -> int:
    return int((value() if callable(value) else value) or 0)


class Admission:
    """The deployment's one gateway semaphore, with lanes. See the module."""

    def __init__(
        self,
        *,
        limit: Setting,
        client: Callable[[], Any],
        interactive_reserve: Setting = 0,
        processes: Setting = 1,
        shares_quota: Callable[[str | None], bool] | None = None,
        name: str = "llm-gateway",
        lease_seconds: float = LEASE_SECONDS,
        poll_seconds: float = 0.05,
        prefix: str = PREFIX,
        semaphore_class: type[WideSemaphore] = WideSemaphore,
    ) -> None:
        self._limit = limit
        self._client = client
        self._reserve = interactive_reserve
        self._processes = processes
        self._shares_quota = shares_quota
        self.name = name
        self._lease = lease_seconds
        self._poll = poll_seconds
        self._prefix = prefix
        self._semaphore_class = semaphore_class
        self._semaphore: tuple[tuple[int, int], WideSemaphore] | None = None

    def enabled(self) -> bool:
        return _read(self._limit) > 0

    def shares_quota(self, provider: str | None) -> bool:
        """Whether a client of ``provider`` spends the chat gateway's quota.
        Without a host rule, every provider does."""
        return True if self._shares_quota is None else bool(self._shares_quota(provider))

    def _hold_args(self, which: Lane, total: int) -> dict:
        """How a lane holds: interactive waits on the list bulk gives way to."""
        reserve = min(max(0, _read(self._reserve)), total - 1)
        if reserve <= 0:
            return {}
        if which == "bulk":
            return {"give_way": reserve}
        return {"urgent": True}

    def _fallback_share(self, total: int) -> int:
        return max(1, total // max(1, _read(self._processes)))

    def semaphore(self) -> WideSemaphore:
        """The one semaphore, rebuilt only if its settings changed."""
        total = _read(self._limit)
        shape = (total, self._fallback_share(total))
        if self._semaphore is None or self._semaphore[0] != shape:
            self._semaphore = (
                shape,
                self._semaphore_class(
                    self.name,
                    total,
                    lease_seconds=self._lease,
                    client=self._client,
                    poll_seconds=self._poll,
                    fallback_limit=shape[1],
                    prefix=self._prefix,
                ),
            )
        return self._semaphore[1]

    def reset(self) -> None:
        """Drop the built semaphore (tests, a client change)."""
        self._semaphore = None

    @asynccontextmanager
    async def slot(self, name: str | None = None, *, provider: str | None = None) -> AsyncIterator[None]:
        """Hold one of the deployment's model-request slots for the block.

        ``provider`` is for a client that does not go through the guarded
        chat client: it is admitted only when it spends the same quota."""
        if not self.enabled() or (provider is not None and not self.shares_quota(provider)):
            yield
            return
        which = _lane.get()
        sem = self.semaphore()
        started = time.perf_counter()
        async with sem.hold(**self._hold_args(which, sem.limit)):
            waited = time.perf_counter() - started
            if waited > 1:
                log.info("llm_admission name=%s lane=%s waited=%.2fs", name, which, waited)
            yield


__all__ = ["LEASE_SECONDS", "Admission", "Lane", "current_lane", "lane"]
