"""The client's requests, exactly as UAE PASS documents them, and how each of
its answers is reported."""

from __future__ import annotations

import base64
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from asas_uaepass import (
    ACR_VALUES,
    SCOPE,
    UaePassClient,
    UaePassRejectedError,
    UaePassSettings,
    UaePassUnavailableError,
    UaePassUnconfiguredError,
)

from .conftest import BASE, CALLBACK, CLIENT_ID, CLIENT_SECRET, FakeUaePass, citizen, client, settings


def test_the_authorize_url_is_uae_pass_s_documented_one(fake: FakeUaePass) -> None:
    url = client(fake).authorization_url(state="nonce-1", locale="ar")
    parsed = urlparse(url)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == f"{BASE}/idshub/authorize"
    assert {k: v[0] for k, v in parse_qs(parsed.query).items()} == {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "scope": SCOPE,
        "state": "nonce-1",
        "redirect_uri": CALLBACK,
        "acr_values": ACR_VALUES,
        "ui_locales": "ar",
    }


def test_any_locale_but_arabic_is_english(fake: FakeUaePass) -> None:
    assert "ui_locales=en" in client(fake).authorization_url(state="s", locale="fr")
    assert "ui_locales=en" in client(fake).authorization_url(state="s")


async def test_the_code_is_swapped_as_uae_pass_s_postman_collection_does(fake: FakeUaePass) -> None:
    c = client(fake)
    profile = await c.sign_in(code=fake.code_for(citizen()))
    assert profile.uuid == "7a05992e-3244-49d3-bcbc-7894c8fca25e"

    token_request, userinfo_request = fake.requests
    assert token_request.method == "POST"
    assert token_request.url.path == "/idshub/token"
    query = parse_qs(token_request.url.query.decode())
    assert query["grant_type"] == ["authorization_code"]
    assert query["redirect_uri"] == [CALLBACK]
    basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    assert token_request.headers["authorization"] == f"Basic {basic}"
    assert token_request.content == b""
    assert "json" not in token_request.headers.get("content-type", "")
    assert token_request.headers["user-agent"] == "asas-uaepass"

    assert userinfo_request.method == "GET"
    assert userinfo_request.url.path == "/idshub/userinfo"
    assert userinfo_request.headers["authorization"].startswith("Bearer ")
    await c.aclose()


async def test_an_unknown_code_is_rejected_and_never_retried(fake: FakeUaePass) -> None:
    with pytest.raises(UaePassRejectedError) as raised:
        await client(fake).sign_in(code="not-a-code")
    assert raised.value.reason == "token_400"
    assert raised.value.code == "uaepass_rejected" and raised.value.http_status == 401
    assert len(fake.requests) == 1


async def test_our_own_credentials_refused_is_its_own_reason(fake: FakeUaePass) -> None:
    with pytest.raises(UaePassRejectedError) as raised:
        await client(fake, client_secret="wrong").sign_in(code=fake.code_for(citizen()))
    assert raised.value.reason == "token_401"


async def test_uae_pass_down_is_unavailable(fake: FakeUaePass) -> None:
    fake.token_answer = lambda request: httpx.Response(503)
    with pytest.raises(UaePassUnavailableError) as raised:
        await client(fake).sign_in(code="c")
    assert raised.value.reason == "token_5xx"
    assert raised.value.http_status == 502


async def test_a_token_timeout_is_unavailable_and_not_retried(fake: FakeUaePass) -> None:
    calls = []

    def slow(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ReadTimeout("slow", request=request)

    fake.token_answer = slow
    with pytest.raises(UaePassUnavailableError) as raised:
        await client(fake).sign_in(code="c")
    assert raised.value.reason == "timeout"
    assert len(calls) == 1


async def test_a_token_body_without_a_token_is_rejected(fake: FakeUaePass) -> None:
    fake.token_answer = lambda request: httpx.Response(200, json={"token_type": "Bearer"})
    with pytest.raises(UaePassRejectedError) as raised:
        await client(fake).sign_in(code="c")
    assert raised.value.reason == "token_shape"


async def test_user_information_is_retried_once_when_the_connection_fails(fake: FakeUaePass) -> None:
    failures = {"left": 1}
    real = fake._userinfo

    def flaky(request: httpx.Request) -> httpx.Response:
        if failures["left"]:
            failures["left"] -= 1
            raise httpx.ConnectError("reset", request=request)
        return real(request)

    fake.userinfo_answer = flaky
    profile = await client(fake).sign_in(code=fake.code_for(citizen()))
    assert profile.user_type == "SOP3"


async def test_user_information_down_twice_is_unavailable(fake: FakeUaePass) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("reset", request=request)

    fake.userinfo_answer = down
    with pytest.raises(UaePassUnavailableError) as raised:
        await client(fake).sign_in(code=fake.code_for(citizen()))
    assert raised.value.reason == "connect"
    assert len(fake.requests) == 3  # the token call, then two tries


async def test_a_token_uae_pass_no_longer_accepts_is_rejected(fake: FakeUaePass) -> None:
    fake.userinfo_answer = lambda request: httpx.Response(
        401, json={"error": "invalid_token", "error_description": "Access token validation failed"}
    )
    with pytest.raises(UaePassRejectedError) as raised:
        await client(fake).sign_in(code=fake.code_for(citizen()))
    assert raised.value.reason == "userinfo"


async def test_user_information_that_is_not_json_is_rejected(fake: FakeUaePass) -> None:
    fake.userinfo_answer = lambda request: httpx.Response(200, text="<html>")
    with pytest.raises(UaePassRejectedError) as raised:
        await client(fake).sign_in(code=fake.code_for(citizen()))
    assert raised.value.reason == "userinfo_shape"


@pytest.mark.parametrize("missing", ["base_url", "client_id", "client_secret", "callback_url"])
async def test_unconfigured_fails_before_any_call(fake: FakeUaePass, missing: str) -> None:
    c = client(fake, **{missing: ""})
    assert not c.settings.configured and c.settings.missing == (missing,)
    with pytest.raises(UaePassUnconfiguredError) as raised:
        c.authorization_url(state="s")
    assert missing in str(raised.value)
    assert raised.value.http_status == 503
    with pytest.raises(UaePassUnconfiguredError):
        await c.sign_in(code="c")
    assert fake.requests == []


def test_a_client_is_built_even_when_unconfigured() -> None:
    """A deployment with UAE PASS off still boots; every call fails as
    unconfigured rather than the import."""
    c = UaePassClient(UaePassSettings())
    with pytest.raises(UaePassUnconfiguredError):
        c.logout_url(redirect_uri="https://app.example/")


def test_logout_sends_the_browser_back_where_the_host_says(fake: FakeUaePass) -> None:
    url = client(fake).logout_url(redirect_uri="https://portal.example/login?signed_out=1")
    parsed = urlparse(url)
    assert parsed.path == "/idshub/logout"
    assert parse_qs(parsed.query) == {"redirect_uri": ["https://portal.example/login?signed_out=1"]}


async def test_a_host_supplied_http_client_is_the_hosts_to_close(fake: FakeUaePass) -> None:
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handle))
    async with UaePassClient(settings(), http=http) as c:
        await c.sign_in(code=fake.code_for(citizen()))
    assert not http.is_closed
    await http.aclose()


async def test_nothing_secret_reaches_the_log(fake: FakeUaePass, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("DEBUG")
    code = fake.code_for(citizen())
    await client(fake).sign_in(code=code)
    with pytest.raises(UaePassRejectedError):
        await client(fake, client_secret="wrong").sign_in(code=fake.code_for(citizen()))
    lines = [record.getMessage() for record in caplog.records]
    text = "\n".join(lines)
    assert any("/idshub/token?[redacted]" in line for line in lines)
    assert code not in text
    basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    assert basic not in text
    assert "784189014978983" not in text
    assert "ramabc1234" not in text
