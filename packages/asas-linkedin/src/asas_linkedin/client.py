"""Every call a host makes to LinkedIn. One client per process.

Request shapes are LinkedIn's documented ones, cited per method:

- OpenID Connect: "Sign In with LinkedIn using OpenID Connect" and
  "Authorization Code Flow (3-legged OAuth)" on Microsoft Learn. Endpoints,
  issuer and keys come from LinkedIn's discovery document at runtime.
- Page posts: "Posts API" (Community Management, ms.date 2026-05-07) with the
  versioned REST headers LinkedIn's own client sends
  (linkedin-api-python-client, restli/utils/api.py).

Built even when unconfigured: each method checks the app it needs and raises
``linkedin_unconfigured`` itself, so a status route can always answer and
nothing fails at import. Tokens are never logged.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

from .errors import (
    LinkedInPageNotConnectedError,
    LinkedInRejectedError,
    LinkedInUnavailableError,
    LinkedInUnconfiguredError,
)
from .identity import LinkedInMember, member_from_claims
from .oidc import JwksFetcher, JwksKeySet, OidcError, decode_verified
from .settings import PAGE_SCOPES, SIGN_IN_SCOPES, AppCredentials, LinkedInSettings

logger = logging.getLogger("asas_linkedin")

DISCOVERY_CACHE_TTL_S = 3600.0
#: Clock skew allowed on LinkedIn's ID token (iat, exp): LinkedIn's clock and
#: the host's are never the same, and without leeway one second of drift
#: refuses a good sign-in.
ID_TOKEN_LEEWAY_S = 60
_REQUIRED_DISCOVERY_FIELDS = ("issuer", "authorization_endpoint", "token_endpoint", "jwks_uri")


@dataclass(frozen=True, slots=True)
class Discovery:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str


@dataclass(frozen=True, slots=True)
class TokenGrant:
    """A token response. ``refresh_token`` only when LinkedIn grants
    programmatic refresh to the app ("Refresh Tokens with OAuth 2.0")."""

    access_token: str
    expires_in: int
    refresh_token: str | None
    refresh_token_expires_in: int | None
    scope: str


def _error_code(response: httpx.Response) -> str:
    """LinkedIn's machine-readable reason, from either error shape it uses:
    OAuth ({"error": ...}) or REST ({"serviceErrorCode": ..., "code": ...})."""
    try:
        body = response.json()
    except ValueError:
        return f"http_{response.status_code}"
    if isinstance(body, dict):
        for key in ("error", "code", "serviceErrorCode"):
            value = body.get(key)
            if value not in (None, ""):
                return str(value)
    return f"http_{response.status_code}"


class LinkedInClient:
    """``http`` lets the host inject its own ``httpx.AsyncClient`` (a shared
    pool, a test transport); a client the host passes in is the host's to
    close. ``transport`` is the lighter alternative for a test."""

    def __init__(
        self,
        settings: LinkedInSettings,
        *,
        http: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._transport = transport
        self._http = http
        self._owns_http = http is None
        self._discovery: Discovery | None = None
        self._discovery_at = 0.0
        self._keys: JwksKeySet | None = None

    @property
    def settings(self) -> LinkedInSettings:
        return self._settings

    # -- configuration --------------------------------------------------------

    def sign_in_app(self) -> AppCredentials:
        app = self._settings.sign_in
        if not app.configured:
            raise LinkedInUnconfiguredError("LinkedIn sign-in has no app configured")
        return app

    def page_app(self) -> AppCredentials:
        app = self._settings.page
        if not (app.configured and self._settings.company_id):
            raise LinkedInUnconfiguredError("The LinkedIn Page app is not configured")
        return app

    # -- transport --------------------------------------------------------------

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=float(self._settings.timeout_seconds),
                transport=self._transport,
                headers={"User-Agent": self._settings.user_agent},
            )
        return self._http

    async def aclose(self) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    async def __aenter__(self) -> LinkedInClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def _send(self, request: httpx.Request) -> httpx.Response:
        try:
            return await self._client().send(request)
        except httpx.TimeoutException as exc:
            raise LinkedInUnavailableError("LinkedIn did not answer in time") from exc
        except httpx.TransportError as exc:
            raise LinkedInUnavailableError("LinkedIn could not be reached") from exc

    # -- OpenID Connect -------------------------------------------------------

    async def discovery(self) -> Discovery:
        """LinkedIn's discovery document, cached for an hour."""
        if self._discovery and time.monotonic() - self._discovery_at < DISCOVERY_CACHE_TTL_S:
            return self._discovery
        response = await self._send(
            self._client().build_request("GET", self._settings.discovery_url)
        )
        if response.status_code != 200:
            raise LinkedInUnavailableError(f"LinkedIn discovery answered {response.status_code}")
        try:
            doc = response.json()
        except ValueError as exc:
            raise LinkedInUnavailableError("LinkedIn discovery is not JSON") from exc
        missing = [f for f in _REQUIRED_DISCOVERY_FIELDS if not isinstance(doc.get(f), str)]
        if missing:
            raise LinkedInUnavailableError(f"LinkedIn discovery lacks {', '.join(missing)}")
        discovery = Discovery(
            issuer=doc["issuer"],
            authorization_endpoint=doc["authorization_endpoint"],
            token_endpoint=doc["token_endpoint"],
            jwks_uri=doc["jwks_uri"],
        )
        if self._discovery is None or self._discovery.jwks_uri != discovery.jwks_uri:
            self._keys = JwksKeySet(self._jwks_fetcher(discovery.jwks_uri))
        self._discovery, self._discovery_at = discovery, time.monotonic()
        return discovery

    def _jwks_fetcher(self, jwks_uri: str) -> JwksFetcher:
        async def fetch() -> dict[str, Any]:
            response = await self._send(self._client().build_request("GET", jwks_uri))
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or "keys" not in payload:
                raise ValueError("JWKS document has no keys")
            return payload

        return fetch

    async def authorization_url(
        self,
        *,
        app: AppCredentials,
        state: str,
        scopes: tuple[str, ...] = SIGN_IN_SCOPES,
    ) -> str:
        """Authorization request ("Authorization Code Flow", Step 2):
        response_type=code, client_id, redirect_uri, state, space-separated scope."""
        discovery = await self.discovery()
        query = urlencode(
            {
                "response_type": "code",
                "client_id": app.client_id,
                "redirect_uri": app.redirect_uri,
                "state": state,
                "scope": " ".join(scopes),
            }
        )
        return f"{discovery.authorization_endpoint}?{query}"

    async def exchange_code(self, *, app: AppCredentials, code: str) -> dict[str, Any]:
        """Exchange the authorization code ("Authorization Code Flow", Step 3):
        POST, x-www-form-urlencoded, the secret in the BODY (client_secret_post;
        LinkedIn documents no Basic auth). The code is single use, so no retry."""
        discovery = await self.discovery()
        response = await self._send(
            self._client().build_request(
                "POST",
                discovery.token_endpoint,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": app.client_id,
                    "client_secret": app.client_secret,
                    "redirect_uri": app.redirect_uri,
                },
                headers={"Accept": "application/json"},
            )
        )
        return self._token_body(response)

    async def refresh(self, *, app: AppCredentials, refresh_token: str) -> dict[str, Any]:
        """Programmatic refresh ("Refresh Tokens with OAuth 2.0"):
        grant_type=refresh_token with the app's id and secret."""
        discovery = await self.discovery()
        response = await self._send(
            self._client().build_request(
                "POST",
                discovery.token_endpoint,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": app.client_id,
                    "client_secret": app.client_secret,
                },
                headers={"Accept": "application/json"},
            )
        )
        return self._token_body(response)

    @staticmethod
    def _token_body(response: httpx.Response) -> dict[str, Any]:
        if response.status_code >= 500:
            raise LinkedInUnavailableError(f"LinkedIn token endpoint answered {response.status_code}")
        if response.status_code != 200:
            reason = _error_code(response)
            logger.warning("linkedin.token_refused status=%s reason=%s", response.status_code, reason)
            raise LinkedInRejectedError("LinkedIn refused the request", details={"reason": reason})
        try:
            body = response.json()
        except ValueError as exc:
            raise LinkedInUnavailableError("LinkedIn token answer is not JSON") from exc
        if not isinstance(body, dict) or not body.get("access_token"):
            raise LinkedInRejectedError("LinkedIn returned no access token")
        return body

    async def verify_id_token(self, id_token: str, *, app: AppCredentials) -> dict[str, Any]:
        """RS256 against LinkedIn's published keys; issuer from the LIVE
        discovery document; audience = the app's client id; exp, iat and sub
        required."""
        discovery = await self.discovery()
        assert self._keys is not None  # discovery() always sets the key set
        try:
            return await decode_verified(
                id_token,
                keys=self._keys,
                audience=app.client_id,
                issuer=discovery.issuer,
                require=("exp", "iat", "iss", "aud", "sub"),
                leeway_s=ID_TOKEN_LEEWAY_S,
            )
        except OidcError as exc:
            # LinkedIn being unreachable while its keys are fetched is "try
            # again later", not "your sign-in was refused".
            if isinstance(exc.cause, LinkedInUnavailableError):
                raise exc.cause from exc
            logger.warning("linkedin.id_token_rejected reason=%s", exc.reason)
            raise LinkedInRejectedError(
                "LinkedIn's ID token did not verify", details={"reason": exc.reason}
            ) from exc

    async def sign_in(self, *, code: str) -> LinkedInMember:
        """The whole server-side half of a LinkedIn sign-in: exchange the
        code, verify the ID token, return the member. Both tokens are dropped
        here; nothing LinkedIn issued is kept."""
        app = self.sign_in_app()
        body = await self.exchange_code(app=app, code=code)
        id_token = body.get("id_token")
        if not isinstance(id_token, str) or not id_token:
            raise LinkedInRejectedError(
                "LinkedIn returned no ID token (is the openid scope on the app?)"
            )
        claims = await self.verify_id_token(id_token, app=app)
        return member_from_claims(claims)

    # -- the Page ---------------------------------------------------------------

    async def page_authorization_url(self, *, state: str) -> str:
        """Where a Page administrator signs in to connect the Page."""
        return await self.authorization_url(app=self.page_app(), state=state, scopes=PAGE_SCOPES)

    async def page_grant(self, *, code: str) -> TokenGrant:
        """The Page administrator's token, from the Page app's own callback."""
        return _grant(await self.exchange_code(app=self.page_app(), code=code))

    async def refresh_page_grant(self, *, refresh_token: str) -> TokenGrant:
        return _grant(await self.refresh(app=self.page_app(), refresh_token=refresh_token))

    # -- Posts API ------------------------------------------------------------

    async def create_post(self, *, access_token: str, body: dict[str, Any]) -> str:
        """POST /rest/posts; returns the new post's URN from the
        ``x-restli-id`` header (HTTP 201). Never retried: LinkedIn's create is
        not idempotent, so a lost answer must not post twice."""
        s = self._settings
        response = await self._send(
            self._client().build_request(
                "POST",
                f"{s.api_base_url}/rest/posts",
                json=body,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "LinkedIn-Version": s.api_version,
                    "X-Restli-Protocol-Version": "2.0.0",
                    "Content-Type": "application/json",
                },
            )
        )
        if response.status_code == 201:
            urn = str(response.headers.get("x-restli-id", ""))
            if not urn:
                raise LinkedInUnavailableError("LinkedIn created the post but returned no id")
            return urn
        reason = _error_code(response)
        logger.warning("linkedin.post_refused status=%s reason=%s", response.status_code, reason)
        if response.status_code == 401:
            raise LinkedInPageNotConnectedError(
                "LinkedIn no longer accepts the Page token", details={"reason": reason}
            )
        if response.status_code == 429 or response.status_code >= 500:
            raise LinkedInUnavailableError(
                "LinkedIn could not take the post now", details={"reason": reason}
            )
        raise LinkedInRejectedError("LinkedIn refused the post", details={"reason": reason})


def _grant(body: dict[str, Any]) -> TokenGrant:
    def _int(key: str) -> int | None:
        value = body.get(key)
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    if not body.get("access_token"):
        raise LinkedInRejectedError("LinkedIn's token response has no access token")
    return TokenGrant(
        access_token=str(body["access_token"]),
        expires_in=_int("expires_in") or 0,
        refresh_token=str(body["refresh_token"]) if body.get("refresh_token") else None,
        refresh_token_expires_in=_int("refresh_token_expires_in"),
        scope=str(body.get("scope") or ""),
    )
