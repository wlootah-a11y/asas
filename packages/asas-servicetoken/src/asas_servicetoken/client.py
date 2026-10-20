"""Sending a signed call with httpx, and sending it once more if refused.

Needs the ``httpx`` extra. The package imports nothing from it until this
module is used.

A refusal (401 or 403) has three causes that look identical from the caller
(one failed call) and are told apart only by logs: the two clocks are too far
apart, the two secrets differ, or no header was sent. Only the first is
transient, and even that one is often confined to ONE replica of the peer
(one bad clock, one replica still on the revision from before a fix) while an
ingress balances per request. So a refused call is sent ONCE more, with a
token stamped further back, which is what lets it clear a clock the first
token could not. Re-signing alone would not: inside one second the claims are
identical and the "fresh" token is the refused one byte for byte.

Bounded at one retry on purpose: a settled misconfiguration must still
surface as a failure rather than as a service that is merely slower. It is
safe because a peer refuses in its auth dependency, before any route body
runs, so the refused request had no effect to repeat.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import httpx

from .tokens import Issuer

UNAUTHORIZED = (401, 403)
"""The statuses read as a refused token.

403 belongs beside 401: FastAPI's ``HTTPBearer`` answers 403 when the header
is absent altogether, which is exactly what an unset secret produces, and it
is the one refusal whose cause is on the caller's side.
"""

_logger = logging.getLogger("asas_servicetoken")


def _url(response: httpx.Response) -> str:
    try:
        return str(response.request.url)
    except RuntimeError:  # a Response built without a request
        return "<unknown url>"


def log_refusal(
    issuer: Issuer,
    response: httpx.Response,
    *,
    logger: logging.Logger | None = None,
) -> None:
    """State what this side signed with when the peer refuses the token.

    No-ops on anything but a 401/403, so a caller can call it on any failed
    response without a branch of its own. It logs NO response body: a caller
    may have posted something the peer echoes back on failure, so whether a
    body is logged is the caller's decision, never this helper's.
    """
    if response.status_code not in UNAUTHORIZED:
        return
    (logger or _logger).error(
        "The peer refused the service token (HTTP %s from %s). %s. Read the"
        " peer's own detail: an immature-iat refusal means this clock leads"
        " the peer's by more than its leeway, a signature refusal means the"
        " two secrets differ, and a missing-credentials refusal means no"
        " header was sent.",
        response.status_code,
        _url(response),
        issuer.describe(),
    )


async def send_with_service_token(
    issuer: Issuer,
    send: Callable[[dict[str, str]], Awaitable[httpx.Response]],
    *,
    retry_backdate_s: int | None = None,
    logger: logging.Logger | None = None,
) -> httpx.Response:
    """Send a call with a fresh token, and once more if the token is refused.

    ``send`` takes the headers and performs the call, so the second attempt
    carries its OWN token, stamped ``retry_backdate_s`` further back (the
    token's lifetime by default: enough to clear a drift of that plus the
    peer's leeway, at no cost in validity since ``exp`` is measured from now).
    """
    log = logger or _logger
    response = await send(issuer.headers())
    if response.status_code not in UNAUTHORIZED:
        return response

    log_refusal(issuer, response, logger=log)
    deeper = issuer.ttl_s if retry_backdate_s is None else retry_backdate_s
    retry = await send(issuer.headers(extra_backdate_s=deeper))
    if retry.status_code not in UNAUTHORIZED:
        # Worth its own line: the refusal was transient or confined to one
        # replica, which is the difference between a clock to chase and a
        # rollout to finish.
        log.warning(
            "The peer accepted the service token on a second attempt (%s), so"
            " the refusal above was not a settled misconfiguration.",
            _url(retry),
        )
    return retry


@asynccontextmanager
async def stream_with_service_token(
    issuer: Issuer,
    client: httpx.AsyncClient,
    build: Callable[[dict[str, str]], httpx.Request],
    *,
    retry_backdate_s: int | None = None,
    logger: logging.Logger | None = None,
) -> AsyncIterator[httpx.Response]:
    """``send_with_service_token`` for a response read as it arrives.

    The other function returns a response whose body is already in memory,
    which is right for JSON and wrong for a recording or a file. ``build``
    takes the headers and returns a REQUEST rather than sending one, because
    a retry needs a second request object (httpx will not re-send a request
    whose stream is spent) and only the caller knows how to rebuild it.

    The first response is closed before the retry goes out. Left open it
    would hold a pooled connection for the whole of the second attempt, and
    under any real concurrency that is how a refusing peer exhausts the pool.
    """
    log = logger or _logger
    response = await client.send(build(issuer.headers()), stream=True)
    if response.status_code not in UNAUTHORIZED:
        try:
            yield response
        finally:
            await response.aclose()
        return

    log_refusal(issuer, response, logger=log)
    await response.aclose()
    deeper = issuer.ttl_s if retry_backdate_s is None else retry_backdate_s
    retry = await client.send(build(issuer.headers(extra_backdate_s=deeper)), stream=True)
    if retry.status_code not in UNAUTHORIZED:
        log.warning(
            "The peer accepted the service token on a second attempt (%s), so"
            " the refusal above was not a settled misconfiguration.",
            _url(retry),
        )
    try:
        yield retry
    finally:
        await retry.aclose()
