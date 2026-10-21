"""UAE PASS at the HTTP edge, for tests: an ``httpx.MockTransport`` handler
answering with the shapes UAE PASS's staging service returned live on
2026-10-06 (an unknown code is 400 ``invalid_grant``, a wrong secret 401
``invalid_client``, a JSON body 500, a bad token 401 ``invalid_token``) and
user information in the shapes of UAE PASS's own sample responses (Web
Application guide, "3. User Information"). Nothing here touches the network.
"""

from __future__ import annotations

import base64
import secrets
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from asas_uaepass import UaePassClient, UaePassSettings

BASE = "https://stg-id.uaepass.ae"
CLIENT_ID = "sandbox_stage"
CLIENT_SECRET = "sandbox_stage"
CALLBACK = "https://app.example/auth/uaepass/callback"


def citizen(**overrides: Any) -> dict[str, Any]:
    """UAE PASS's own "Citizen/Resident Profile" sample, as published."""
    profile: dict[str, Any] = {
        "sub": "UAEPASS/7a05992e-3244-49d3-bcbc-7894c8fca25e",
        "fullnameAR": "ساوميا,,,,شارما,,",
        "gender": "Male",
        "mobile": "97151234003",
        "lastnameEN": "ABC",
        "fullnameEN": "Ram,,,,ABC,,",
        "uuid": "7a05992e-3244-49d3-bcbc-7894c8fca25e",
        "lastnameAR": "شارما",
        "idn": "784189014978983",
        "nationalityEN": "IND",
        "firstnameEN": "Ram",
        "userType": "SOP3",
        "nationalityAR": "هندى",
        "firstnameAR": "ساوميا",
        "email": "ramabc1234@gmail.com",
    }
    profile.update(overrides)
    return {k: v for k, v in profile.items() if v is not None}


def visitor(**overrides: Any) -> dict[str, Any]:
    """UAE PASS's own "Visitor Profile" sample (its ``unifiedID`` spelling and
    upper-case full name included)."""
    profile: dict[str, Any] = {
        "sub": "35q00600-27a0-5555-8d93-453392902b84",
        "fullnameAR": "عمر,بدوى,حسنين,,عبدالله,,",
        "mobile": "971566612311",
        "fullnameEN": "AMAR,,,,KHAN,,",
        "uuid": "35q87600-27a0-5555-8d93-453392902b84",
        "profileType": "2",
        "nationalityEN": "IND",
        "nationalityAR": "هندى",
        "firstnameEN": "AMAR",
        "unifiedID": "123458099",
        "userType": "SOP3",
        "firstnameAR": "عمر",
        "lastnameAR": "شارما",
        "lastnameEN": "Khan",
        "email": "Khan@dubai.ae",
    }
    profile.update(overrides)
    return {k: v for k, v in profile.items() if v is not None}


def basic_user(**overrides: Any) -> dict[str, Any]:
    """An SOP1 account: verified mobile and email, English names, nothing
    from an Emirates ID (Attributes List)."""
    profile: dict[str, Any] = {
        "uuid": "b1c4d0e2-0000-4000-8000-0000000000a1",
        "userType": "SOP1",
        "email": "fatima.sop1@example.ae",
        "mobile": "971501234567",
        "firstnameEN": "Fatima",
        "lastnameEN": "Al Mansoori",
    }
    profile.update(overrides)
    return {k: v for k, v in profile.items() if v is not None}


class FakeUaePass:
    """``/idshub/token`` and ``/idshub/userinfo``. A test registers a person
    with ``code_for`` and comes back from "UAE PASS" with that code."""

    def __init__(self) -> None:
        self._codes: dict[str, dict[str, Any]] = {}
        self._tokens: dict[str, dict[str, Any]] = {}
        self.requests: list[httpx.Request] = []
        #: Replace to inject a fault: ``lambda request: httpx.Response(503)``.
        self.token_answer: Callable[[httpx.Request], httpx.Response] | None = None
        self.userinfo_answer: Callable[[httpx.Request], httpx.Response] | None = None

    def code_for(self, profile: dict[str, Any]) -> str:
        code = f"{secrets.token_hex(4)}-code"
        self._codes[code] = profile
        return code

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/idshub/token":
            if self.token_answer is not None:
                return self.token_answer(request)
            return self._token(request)
        if request.url.path == "/idshub/userinfo":
            if self.userinfo_answer is not None:
                return self.userinfo_answer(request)
            return self._userinfo(request)
        return httpx.Response(404)

    def _token(self, request: httpx.Request) -> httpx.Response:
        if request.method != "POST":
            return httpx.Response(405)
        if "application/json" in request.headers.get("content-type", ""):
            return httpx.Response(500, text="Internal Server Error")
        expected = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
        if request.headers.get("authorization") != f"Basic {expected}":
            return httpx.Response(
                401, json={"error": "invalid_client", "error_description": "invalidCredentials"}
            )
        query = parse_qs(request.url.query.decode())
        code = (query.get("code") or [""])[0]
        profile = self._codes.pop(code, None)  # single use, like the real one
        if profile is None or query.get("grant_type") != ["authorization_code"]:
            return httpx.Response(
                400,
                json={
                    "error": "invalid_grant",
                    "error_description": "Invalid authorization code received from token request",
                },
            )
        token = secrets.token_hex(16)
        self._tokens[token] = profile
        return httpx.Response(
            200,
            json={
                "access_token": token,
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": "urn:uae:digitalid:profile:general",
            },
        )

    def _userinfo(self, request: httpx.Request) -> httpx.Response:
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        profile = self._tokens.get(token)
        if profile is None:
            return httpx.Response(
                401,
                json={"error": "invalid_token", "error_description": "Access token validation failed"},
            )
        return httpx.Response(200, json=profile)


def settings(**overrides: Any) -> UaePassSettings:
    base: dict[str, Any] = dict(
        base_url=BASE,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        callback_url=CALLBACK,
        timeout_seconds=5.0,
        kept_attributes="userType,email,mobile,firstnameEN,lastnameEN,nationalityEN",
    )
    base.update(overrides)
    return UaePassSettings(**base)


def client(fake: FakeUaePass, **overrides: Any) -> UaePassClient:
    return UaePassClient(settings(**overrides), transport=httpx.MockTransport(fake.handle))


@pytest.fixture
def fake() -> FakeUaePass:
    return FakeUaePass()
