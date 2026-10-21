"""OpenID Connect ID-token validation against a provider's published keys.

PyJWT does the cryptography; this module only owns the key set's lifecycle:

- the JWKS is fetched lazily and cached for ``JWKS_CACHE_TTL_S``;
- a token signed by a key id the cache does not hold triggers ONE refresh, so
  a provider's key rollover needs no restart, then fails closed;
- every key is built once per refresh (``PyJWK``), so per-request validation
  is a dict lookup, and one malformed entry cannot break the rest.

Every failure is an :class:`OidcError` with one stable ``reason`` per failure
class; token contents are never echoed. The LinkedIn client maps it to its
own refusal; a host may use the module for any other RS256 provider.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import jwt as pyjwt

#: JWKS cache lifetime. On an unknown kid the cache is refreshed once before
#: failing, so normal key rollover needs no restart.
JWKS_CACHE_TTL_S = 3600.0

#: A JWKS document fetcher: returns the parsed ``{"keys": [...]}`` dict.
#: Injectable so a test validates real signatures with a locally built JWKS.
JwksFetcher = Callable[[], Awaitable[dict[str, Any]]]


class OidcError(Exception):
    """An ID token that will not verify. ``reason`` is stable; ``cause`` is
    the fetcher's own exception when the keys could not be loaded, so a
    caller can tell "the provider is down" from "the token is bad"."""

    def __init__(self, reason: str, *, cause: BaseException | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.cause = cause


class JwksKeySet:
    """A provider's signing keys, cached, refreshed on an unknown key id.

    ``min_refresh_interval_s`` bounds how often an UNKNOWN kid may trigger a
    fetch. Zero (the default) keeps one refresh per unknown kid. A route
    anyone on the internet can reach sets it, so a stream of made-up kids
    cannot turn every request into an outbound fetch of the key document.
    """

    def __init__(
        self,
        fetcher: JwksFetcher,
        *,
        algorithm: str = "RS256",
        min_refresh_interval_s: float = 0.0,
    ) -> None:
        self._fetch = fetcher
        self._algorithm = algorithm
        self._min_refresh_interval_s = min_refresh_interval_s
        self._jwks: dict[str, Any] | None = None
        self._keys: dict[str, Any] = {}
        self._fetched_at = 0.0
        self._attempted_at: float | None = None

    async def signing_key(self, kid: str) -> Any:
        key = self._from_cache(kid)
        if key is None and self._may_refresh():
            await self._refresh()
            key = self._from_cache(kid)
        if key is None:
            raise OidcError("unknown_key")
        return key

    def _from_cache(self, kid: str) -> Any:
        if self._jwks is None or time.monotonic() - self._fetched_at > JWKS_CACHE_TTL_S:
            return None
        return self._keys.get(kid)

    def _may_refresh(self) -> bool:
        if self._attempted_at is None:
            return True
        since_attempt = time.monotonic() - self._attempted_at
        if since_attempt >= self._min_refresh_interval_s:
            return True
        # Inside the floor, refetch only for a cache gone stale. A FAILED first
        # fetch counts as an attempt too: a provider that is down must not
        # turn every request into another outbound fetch.
        return self._jwks is not None and time.monotonic() - self._fetched_at > JWKS_CACHE_TTL_S

    async def _refresh(self) -> None:
        self._attempted_at = time.monotonic()
        try:
            jwks = await self._fetch()
        except Exception as exc:
            raise OidcError("keys_unavailable", cause=exc) from exc
        keys: dict[str, Any] = {}
        for entry in jwks.get("keys", []):
            kid = entry.get("kid")
            if not kid:
                continue
            try:
                keys[str(kid)] = pyjwt.PyJWK(entry, algorithm=self._algorithm).key
            except Exception:  # noqa: BLE001 - skip an unparseable key, keep the rest
                continue
        self._jwks = jwks
        self._keys = keys
        self._fetched_at = time.monotonic()


async def decode_verified(
    token: str,
    *,
    keys: JwksKeySet,
    audience: str,
    issuer: str,
    require: Sequence[str] = ("exp", "aud", "iss"),
    algorithms: Sequence[str] = ("RS256",),
    leeway_s: int = 0,
) -> dict[str, Any]:
    """Verify signature, audience, issuer and expiry; return the claims."""
    try:
        header = pyjwt.get_unverified_header(token)
    except pyjwt.InvalidTokenError as exc:
        raise OidcError("malformed") from exc
    kid = header.get("kid")
    if not kid:
        raise OidcError("no_key_id")
    key = await keys.signing_key(str(kid))
    try:
        claims: dict[str, Any] = pyjwt.decode(
            token,
            key=key,
            algorithms=list(algorithms),
            audience=audience,
            issuer=issuer,
            leeway=leeway_s,
            options={"require": list(require)},
        )
    except pyjwt.InvalidTokenError as exc:
        raise OidcError(type(exc).__name__) from exc
    return claims
