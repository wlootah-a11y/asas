"""The httpx helpers: one retry, stamped further back, and the refusal log.

No network: ``send`` is a recorder, and the streaming tests run an
``httpx.AsyncClient`` over a ``MockTransport``. The peer that verifies is the
package's own ``Verifier``, so the retry is proven against the real check and
not against a status sequence.
"""

from __future__ import annotations

import logging
import time

import httpx
import jwt
import pytest

from asas_servicetoken import (
    UNAUTHORIZED,
    Issuer,
    TokenRefused,
    Verifier,
    bearer_token,
    fingerprint,
    log_refusal,
    send_with_service_token,
    stream_with_service_token,
)

SECRET = "a-shared-secret-of-at-least-thirty-two-bytes"


def issuer(**kw) -> Issuer:
    kw.setdefault("issuer", "core")
    kw.setdefault("audience", "engine")
    return Issuer(SECRET, **kw)


class Recorder:
    """Answers the given statuses in order, recording the token it was sent."""

    def __init__(self, *statuses: int) -> None:
        self._statuses = list(statuses)
        self.tokens: list[str] = []

    async def __call__(self, headers: dict[str, str]) -> httpx.Response:
        self.tokens.append(headers.get("Authorization", ""))
        return httpx.Response(
            self._statuses.pop(0), request=httpx.Request("POST", "https://engine/execute")
        )


async def test_a_refused_call_is_sent_once_more_with_a_fresh_token(caplog) -> None:
    """What survives one bad replica or a clock that just stepped: an ingress
    balances per request, so the second attempt can land elsewhere."""
    send = Recorder(401, 200)

    with caplog.at_level(logging.WARNING, logger="asas_servicetoken"):
        response = await send_with_service_token(issuer(), send)

    assert response.status_code == 200
    assert len(send.tokens) == 2
    assert all(t.startswith("Bearer ") for t in send.tokens)
    assert send.tokens[0] != send.tokens[1], "a fresh token, not the refused one replayed"
    assert "accepted the service token on a second attempt" in caplog.text


async def test_a_settled_refusal_still_fails() -> None:
    """Bounded at one retry: a wrong secret must surface as a failure, never as
    a service that is merely slower."""
    send = Recorder(401, 401)

    response = await send_with_service_token(issuer(), send)

    assert response.status_code == 401
    assert len(send.tokens) == 2


@pytest.mark.parametrize("status", [400, 404, 422, 500, 502, 504])
async def test_anything_but_a_refusal_is_sent_once(status) -> None:
    """A 500 is the peer's answer, not a token problem; retrying it would
    double every real failure."""
    send = Recorder(status)

    response = await send_with_service_token(issuer(), send)

    assert response.status_code == status
    assert len(send.tokens) == 1


async def test_a_403_counts_as_a_refusal() -> None:
    """FastAPI's HTTPBearer answers 403 for a missing header, which is what an
    unset secret on this side produces."""
    send = Recorder(403, 200)

    response = await send_with_service_token(issuer(), send)

    assert response.status_code == 200
    assert 403 in UNAUTHORIZED and 401 in UNAUTHORIZED


async def test_the_retry_is_stamped_a_lifetime_further_back_and_lives_no_longer() -> None:
    send = Recorder(401, 200)

    await send_with_service_token(issuer(clock=lambda: 1_800_000_000), send)

    first = jwt.decode(bearer_token(send.tokens[0]), options={"verify_signature": False})
    second = jwt.decode(bearer_token(send.tokens[1]), options={"verify_signature": False})
    assert second["iat"] == first["iat"] - 300
    assert second["exp"] == first["exp"]


async def test_the_retry_backdate_is_the_callers_to_set() -> None:
    send = Recorder(401, 200)

    await send_with_service_token(issuer(clock=lambda: 1_800_000_000), send, retry_backdate_s=45)

    first = jwt.decode(bearer_token(send.tokens[0]), options={"verify_signature": False})
    second = jwt.decode(bearer_token(send.tokens[1]), options={"verify_signature": False})
    assert second["iat"] == first["iat"] - 45


async def test_the_retry_clears_a_clock_the_first_attempt_could_not() -> None:
    """The point of the deeper backdate, driven through the peer's REAL check.

    One verifier, one clock 200 s behind the issuer's and 60 s of leeway: the
    first token's iat is 170 s in the verifier's future and refused, the
    retry's is 130 s in its past and accepted. Re-signing alone would have
    produced the refused token again.
    """
    ahead = issuer(clock=lambda: time.time() + 200)
    peer = Verifier(SECRET, issuer="core", audience="engine", leeway_s=60)
    seen: list[str] = []

    async def engine(headers: dict[str, str]) -> httpx.Response:
        token = bearer_token(headers.get("Authorization"))
        seen.append(token)
        request = httpx.Request("POST", "https://engine/execute")
        try:
            peer.verify(token)
        except TokenRefused as refused:
            assert refused.reason == "immature"
            return httpx.Response(401, request=request)
        return httpx.Response(200, request=request)

    response = await send_with_service_token(ahead, engine)

    assert response.status_code == 200, "the retry must clear the drift"
    assert len(seen) == 2 and seen[0] != seen[1]


# ── the refusal log ──────────────────────────────────────────────────────────


def test_a_refusal_reports_our_clock_and_which_secret_we_signed_with(caplog) -> None:
    with caplog.at_level(logging.ERROR, logger="asas_servicetoken"):
        log_refusal(
            issuer(),
            httpx.Response(
                401,
                text='{"detail": "The token is not yet valid (iat)"}',
                request=httpx.Request("POST", "https://engine/execute"),
            ),
        )

    assert "refused the service token" in caplog.text
    assert "https://engine/execute" in caplog.text
    assert fingerprint(SECRET) in caplog.text
    assert SECRET not in caplog.text
    assert "not yet valid" not in caplog.text, "never the response body"


def test_a_refusal_names_the_unset_secret_rather_than_fingerprinting_nothing(caplog) -> None:
    with caplog.at_level(logging.ERROR, logger="asas_servicetoken"):
        log_refusal(Issuer("", issuer="core"), httpx.Response(403))

    assert "<unset>" in caplog.text
    assert "<unknown url>" in caplog.text


def test_only_a_refusal_is_diagnosed(caplog) -> None:
    """Callers call this on any failed response, so it must no-op on the
    statuses that have nothing to do with the token."""
    with caplog.at_level(logging.ERROR, logger="asas_servicetoken"):
        for status in (400, 404, 422, 500, 502, 504):
            log_refusal(issuer(), httpx.Response(status))

    assert caplog.text == ""


# ── streaming ────────────────────────────────────────────────────────────────


class StreamingPeer:
    """A transport answering the given statuses in order, tracking which
    responses the caller closed."""

    def __init__(self, *statuses: int) -> None:
        self._statuses = list(statuses)
        self.tokens: list[str] = []
        self.closed: list[int] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.tokens.append(bearer_token(request.headers.get("Authorization")))
        status = self._statuses.pop(0)
        index = len(self.tokens) - 1

        async def body():
            yield b"audio-bytes"

        response = httpx.Response(status, stream=_Stream(body(), lambda: self.closed.append(index)))
        return response


class _Stream(httpx.AsyncByteStream):
    def __init__(self, chunks, on_close) -> None:
        self._chunks = chunks
        self._on_close = on_close

    async def __aiter__(self):
        async for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        self._on_close()


async def test_a_streamed_response_is_yielded_unread() -> None:
    peer = StreamingPeer(200)
    async with httpx.AsyncClient(transport=httpx.MockTransport(peer.handler)) as client:
        async with stream_with_service_token(
            issuer(), client, lambda h: client.build_request("GET", "https://engine/recording", headers=h)
        ) as response:
            assert response.status_code == 200
            assert b"".join([c async for c in response.aiter_bytes()]) == b"audio-bytes"
    assert peer.closed == [0]


async def test_a_refused_stream_is_closed_before_the_retry_goes_out() -> None:
    """Left open, the first response holds a pooled connection for the whole
    of the second attempt."""
    peer = StreamingPeer(401, 200)
    order: list[str] = []

    async with httpx.AsyncClient(transport=httpx.MockTransport(peer.handler)) as client:

        def build(headers: dict[str, str]) -> httpx.Request:
            order.append("built")
            return client.build_request("GET", "https://engine/recording", headers=headers)

        async with stream_with_service_token(issuer(), client, build) as response:
            assert response.status_code == 200
            assert peer.closed == [0], "the refused response was closed first"
    assert peer.closed == [0, 1]
    assert len(peer.tokens) == 2 and peer.tokens[0] != peer.tokens[1]
    assert order == ["built", "built"]


async def test_a_settled_refusal_on_a_stream_is_yielded_as_the_refusal() -> None:
    peer = StreamingPeer(401, 401)
    async with httpx.AsyncClient(transport=httpx.MockTransport(peer.handler)) as client:
        async with stream_with_service_token(
            issuer(), client, lambda h: client.build_request("GET", "https://engine/recording", headers=h)
        ) as response:
            assert response.status_code == 401
    assert len(peer.tokens) == 2
