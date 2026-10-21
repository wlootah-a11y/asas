"""Every call a service provider makes to UAE PASS.

Request shapes are UAE PASS's documented ones (docs.uaepass.ae, Web
Application guide, read 2026-10-05; the Authentication APIs Postman
Collection V2):

- ``GET  /idshub/authorize``: the browser goes there, never the server.
- ``POST /idshub/token?grant_type&redirect_uri&code`` with HTTP Basic client
  credentials and an EMPTY body, exactly as UAE PASS's own collection sends
  it. A JSON body gets a 500 (checked live against staging, 2026-10-06). The
  answer is an opaque access token good for 3600 seconds.
- ``GET  /idshub/userinfo`` with that token as Bearer.
- ``GET  /idshub/logout?redirect_uri``: the browser again.

UAE PASS publishes no OpenID discovery document or signing keys (all three
standard addresses answer 403), and returns an ID token only "if the scope
was openid requested", so none is asked for: the details are trusted because
they are read from UAE PASS itself, over HTTPS, with a token this client
obtained itself.

The code is swapped at once and never retried: it is single use, and UAE
PASS says it "should be utilized by SP within 10 seconds". The token is
dropped as soon as the user information is read. Neither the code, the
token, the client secret nor any response body is ever logged (Web
Authentication Implementation Checklist V1.3: "no logs or storing of UAE
PASS API data ... except user info data as per business").
"""

from __future__ import annotations

import base64
import logging
from typing import Any
from urllib.parse import urlencode

import httpx

from .errors import UaePassRejectedError, UaePassUnavailableError, UaePassUnconfiguredError
from .profile import UaePassProfile, kept_attributes, parse_userinfo
from .settings import UaePassSettings

logger = logging.getLogger("asas_uaepass")

#: The general profile scope every service provider requests (Web Application
#: guide, "1. Authorization Code"). Visitors' extra scopes are a separate
#: agreement with UAE PASS.
SCOPE = "urn:uae:digitalid:profile:general"
#: The web channel's authentication level. The mobile app's
#: ``urn:digitalid:authentication:flow:mobileondevice`` is for native apps.
ACR_VALUES = "urn:safelayer:tws:policies:authentication:level:low"

AUTHORIZE_PATH = "/idshub/authorize"
TOKEN_PATH = "/idshub/token"
USERINFO_PATH = "/idshub/userinfo"
LOGOUT_PATH = "/idshub/logout"


class _RedactIdshubQuery(logging.Filter):
    """httpx logs every request line at INFO ("HTTP Request: POST <url> ...")
    and UAE PASS takes the authorization code in the token request's QUERY
    STRING, so without this the code would be printed whole by any host that
    logs at INFO. The query is dropped from any ``/idshub/`` URL; every other
    line is left as it was."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 2:
            url = str(args[1])
            if "/idshub/" in url and "?" in url:
                record.args = (args[0], url.split("?", 1)[0] + "?[redacted]", *args[2:])
        return True


_HTTPX_FILTER = _RedactIdshubQuery()


def install_log_redaction() -> None:
    """Keep the authorization code out of httpx's own request log. Called by
    every client at construction; idempotent."""
    httpx_logger = logging.getLogger("httpx")
    if _HTTPX_FILTER not in httpx_logger.filters:
        httpx_logger.addFilter(_HTTPX_FILTER)


def _oauth_error(response: httpx.Response) -> str:
    """UAE PASS's machine-readable ``error`` (``invalid_grant``,
    ``invalid_client``, ``invalid_token``), never its description."""
    try:
        body = response.json()
    except ValueError:
        return ""
    if isinstance(body, dict) and isinstance(body.get("error"), str):
        return str(body["error"])[:64]
    return ""


class UaePassClient:
    """One client per process, built even when unconfigured.

    ``http`` lets the host inject its own ``httpx.AsyncClient`` (a shared
    pool, a test transport); a client the host passes in is the host's to
    close. ``transport`` is the lighter alternative for a test.
    """

    def __init__(
        self,
        settings: UaePassSettings,
        *,
        http: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._transport = transport
        self._http = http
        self._owns_http = http is None
        install_log_redaction()

    @property
    def settings(self) -> UaePassSettings:
        return self._settings

    # -- configuration --------------------------------------------------------

    def configured(self) -> None:
        """Raise :class:`UaePassUnconfiguredError` unless a sign-in can run."""
        missing = self._settings.missing
        if missing:
            raise UaePassUnconfiguredError(
                f"UAE PASS sign-in is not configured (missing {', '.join(missing)})"
            )

    def _url(self, path: str) -> str:
        return f"{self._settings.base_url}{path}"

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

    async def __aenter__(self) -> UaePassClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # -- browser addresses --------------------------------------------------------

    def authorization_url(self, *, state: str, locale: str | None = None) -> str:
        """Where to send the browser. ``state`` is the host's own anti-forgery
        value, checked on the callback; ``locale`` picks UAE PASS's Arabic
        screens for ``"ar"`` and English for anything else."""
        self.configured()
        s = self._settings
        query = {
            "response_type": "code",
            "client_id": s.client_id,
            "scope": SCOPE,
            "state": state,
            "redirect_uri": s.callback_url,
            "acr_values": ACR_VALUES,
            "ui_locales": "ar" if locale == "ar" else "en",
        }
        return f"{self._url(AUTHORIZE_PATH)}?{urlencode(query)}"

    def logout_url(self, *, redirect_uri: str) -> str:
        """UAE PASS's logout, which ends its own session and sends the
        browser to ``redirect_uri`` (Web Application guide, "4. Logout")."""
        if not self._settings.base_url:
            raise UaePassUnconfiguredError("UAE PASS sign-in is not configured (missing base_url)")
        return f"{self._url(LOGOUT_PATH)}?{urlencode({'redirect_uri': redirect_uri})}"

    # -- the exchange ---------------------------------------------------------------

    async def sign_in(self, *, code: str) -> UaePassProfile:
        """Swap the code, read the user information, drop the token."""
        self.configured()
        access_token = await self._token(code)
        body = await self._userinfo(access_token)
        return parse_userinfo(body, kept=kept_attributes(self._settings.kept_attributes))

    async def _token(self, code: str) -> str:
        s = self._settings
        pair = f"{s.client_id}:{s.client_secret}".encode()
        basic = base64.b64encode(pair).decode()
        request = self._client().build_request(
            "POST",
            self._url(TOKEN_PATH),
            params={
                "grant_type": "authorization_code",
                "redirect_uri": s.callback_url,
                "code": code,
            },
            headers={"Authorization": f"Basic {basic}"},
        )
        try:
            response = await self._client().send(request)
        except httpx.TimeoutException as exc:
            raise UaePassUnavailableError("UAE PASS did not answer", reason="timeout") from exc
        except httpx.HTTPError as exc:
            raise UaePassUnavailableError("UAE PASS could not be reached", reason="connect") from exc

        if response.status_code >= 500:
            logger.warning("uaepass.token status=%s", response.status_code)
            raise UaePassUnavailableError("UAE PASS failed", reason="token_5xx")
        if response.status_code == 401:
            # Our own client id or secret was refused: a configuration fault,
            # which an alert on reason=token_401 exists for.
            logger.error("uaepass.token status=401 error=%s", _oauth_error(response))
            raise UaePassRejectedError("UAE PASS refused our credentials", reason="token_401")
        if response.status_code != 200:
            logger.info("uaepass.token status=%s error=%s", response.status_code, _oauth_error(response))
            raise UaePassRejectedError("UAE PASS refused the code", reason="token_400")
        try:
            token = response.json().get("access_token")
        except (ValueError, AttributeError):
            token = None
        if not isinstance(token, str) or not token:
            raise UaePassRejectedError("UAE PASS sent no access token", reason="token_shape")
        return token

    async def _userinfo(self, access_token: str) -> Any:
        request = self._client().build_request(
            "GET", self._url(USERINFO_PATH), headers={"Authorization": f"Bearer {access_token}"}
        )
        response: httpx.Response | None = None
        # One retry, and only when the connection itself failed: nothing was
        # read, and the token is still good for an hour.
        for attempt in (1, 2):
            try:
                response = await self._client().send(request)
                break
            except httpx.ConnectError as exc:
                if attempt == 2:
                    raise UaePassUnavailableError(
                        "UAE PASS could not be reached", reason="connect"
                    ) from exc
            except httpx.TimeoutException as exc:
                raise UaePassUnavailableError("UAE PASS did not answer", reason="timeout") from exc
            except httpx.HTTPError as exc:
                raise UaePassUnavailableError(
                    "UAE PASS could not be reached", reason="connect"
                ) from exc
        assert response is not None
        if response.status_code >= 500:
            logger.warning("uaepass.userinfo status=%s", response.status_code)
            raise UaePassUnavailableError("UAE PASS failed", reason="userinfo_5xx")
        if response.status_code != 200:
            logger.info(
                "uaepass.userinfo status=%s error=%s", response.status_code, _oauth_error(response)
            )
            raise UaePassRejectedError("UAE PASS refused the token", reason="userinfo")
        try:
            return response.json()
        except ValueError as exc:
            raise UaePassRejectedError(
                "UAE PASS user information is not JSON", reason="userinfo_shape"
            ) from exc
