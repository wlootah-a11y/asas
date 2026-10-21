"""The client: LinkedIn's requests exactly as documented, and how each of its
answers is reported. ID tokens are real RS256 tokens, so the validator runs
unchanged."""

from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from asas_linkedin import (
    LinkedInClient,
    LinkedInPageNotConnectedError,
    LinkedInRejectedError,
    LinkedInUnavailableError,
    LinkedInUnconfiguredError,
    member_from_claims,
)

from .conftest import (
    CALLBACK,
    CLIENT_ID,
    DISCOVERY,
    JWKS_URL,
    OTHER_KEY,
    PAGE,
    TOKEN_URL,
    FakeLinkedIn,
    client,
    id_token,
    jwk,
    settings,
)


async def test_sign_in_exchanges_the_code_with_the_secret_in_the_body(linkedin: FakeLinkedIn) -> None:
    member = await client(linkedin).sign_in(code="AQT-code")

    assert member.sub == "782bbtaQ" and member.email == "mariam@example.ae"
    assert member.email_verified is True and member.language == "en"
    token_request = next(r for r in linkedin.requests if str(r.url) == TOKEN_URL)
    form = parse_qs(token_request.content.decode())
    assert form == {
        "grant_type": ["authorization_code"], "code": ["AQT-code"], "client_id": [CLIENT_ID],
        "client_secret": ["secret-1"], "redirect_uri": [CALLBACK],
    }
    assert "authorization" not in {k.lower() for k in token_request.headers}
    assert token_request.headers["user-agent"] == "asas-linkedin"


async def test_the_authorization_url_carries_state_and_the_three_scopes(linkedin: FakeLinkedIn) -> None:
    c = client(linkedin)
    url = await c.authorization_url(app=c.sign_in_app(), state="nonce-1")
    parsed = urlparse(url)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == DISCOVERY["authorization_endpoint"]
    assert parse_qs(parsed.query) == {
        "response_type": ["code"], "client_id": [CLIENT_ID], "redirect_uri": [CALLBACK],
        "state": ["nonce-1"], "scope": ["openid profile email"],
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"iss": "https://www.linkedin.com"},  # the Learn page's issuer, not the live one
        {"aud": "someone-elses-app"},
        {"exp": int(time.time()) - 120},  # past the clock-skew leeway
        {"iat": None},
        {"sub": None},
        {"_key": OTHER_KEY},  # right kid, wrong key: signature fails
    ],
)
async def test_an_id_token_that_does_not_verify_is_rejected(linkedin: FakeLinkedIn, overrides: dict[str, Any]) -> None:
    linkedin.token_answer = lambda r: httpx.Response(200, json={"access_token": "a", "id_token": id_token(**overrides)})
    with pytest.raises(LinkedInRejectedError) as raised:
        await client(linkedin).sign_in(code="c")
    assert raised.value.details["reason"]


async def test_a_token_issued_a_few_seconds_in_our_future_still_verifies(linkedin: FakeLinkedIn) -> None:
    """LinkedIn's clock a little ahead of ours is ordinary drift, not a forgery."""
    linkedin.token_answer = lambda r: httpx.Response(
        200, json={"access_token": "a", "id_token": id_token(iat=int(time.time()) + 5)}
    )
    assert (await client(linkedin).sign_in(code="c")).sub == "782bbtaQ"


async def test_an_unknown_key_id_refetches_the_keys_once_then_fails(linkedin: FakeLinkedIn) -> None:
    linkedin.token_answer = lambda r: httpx.Response(200, json={"access_token": "a", "id_token": id_token(_kid="rotated")})
    with pytest.raises(LinkedInRejectedError) as raised:
        await client(linkedin).sign_in(code="c")
    assert raised.value.details == {"reason": "unknown_key"}
    assert sum(1 for r in linkedin.requests if str(r.url) == JWKS_URL) == 1


async def test_a_rotated_key_is_picked_up_without_a_restart(linkedin: FakeLinkedIn) -> None:
    c = client(linkedin)
    await c.sign_in(code="c")
    linkedin.jwks = {"keys": [jwk(OTHER_KEY, "new-kid")]}
    linkedin.token_answer = lambda r: httpx.Response(
        200, json={"access_token": "a", "id_token": id_token(_key=OTHER_KEY, _kid="new-kid")}
    )
    assert (await c.sign_in(code="c2")).sub == "782bbtaQ"


async def test_the_discovery_document_is_read_once_an_hour(linkedin: FakeLinkedIn) -> None:
    c = client(linkedin)
    await c.sign_in(code="c")
    await c.sign_in(code="c2")
    assert sum(1 for r in linkedin.requests if "openid-configuration" in str(r.url)) == 1


async def test_linkedins_unknown_code_answer_is_a_rejection(linkedin: FakeLinkedIn) -> None:
    """LinkedIn's live answer to an unknown code (drift check, 2026-10-05)."""
    linkedin.token_answer = lambda r: httpx.Response(401, json={
        "error": "invalid_request",
        "error_description": "Unable to retrieve access token: authorization code not found",
    })
    with pytest.raises(LinkedInRejectedError) as raised:
        await client(linkedin).sign_in(code="gone")
    assert raised.value.details == {"reason": "invalid_request"}
    assert raised.value.code == "linkedin_rejected" and raised.value.http_status == 502


async def test_a_token_answer_without_an_id_token_is_rejected(linkedin: FakeLinkedIn) -> None:
    linkedin.token_answer = lambda r: httpx.Response(200, json={"access_token": "a", "expires_in": 60})
    with pytest.raises(LinkedInRejectedError):
        await client(linkedin).sign_in(code="c")


@pytest.mark.parametrize("status", [500, 503])
async def test_linkedin_5xx_is_unavailable_not_rejected(linkedin: FakeLinkedIn, status: int) -> None:
    linkedin.token_answer = lambda r: httpx.Response(status)
    with pytest.raises(LinkedInUnavailableError):
        await client(linkedin).sign_in(code="c")


async def test_a_timeout_is_unavailable(linkedin: FakeLinkedIn) -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    linkedin.token_answer = slow
    with pytest.raises(LinkedInUnavailableError):
        await client(linkedin).sign_in(code="c")


async def test_keys_unreachable_is_unavailable(linkedin: FakeLinkedIn) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == JWKS_URL:
            raise httpx.ConnectError("down", request=request)
        return linkedin(request)

    c = LinkedInClient(settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(LinkedInUnavailableError):
        await c.sign_in(code="c")


async def test_a_discovery_document_missing_an_endpoint_is_unavailable(linkedin: FakeLinkedIn) -> None:
    del linkedin.discovery["jwks_uri"]
    with pytest.raises(LinkedInUnavailableError):
        await client(linkedin).sign_in(code="c")


async def test_sign_in_without_an_app_is_unconfigured(linkedin: FakeLinkedIn) -> None:
    c = LinkedInClient(settings(sign_in=__import__("asas_linkedin").AppCredentials()), transport=httpx.MockTransport(linkedin))
    with pytest.raises(LinkedInUnconfiguredError) as raised:
        await c.sign_in(code="c")
    assert raised.value.http_status == 503
    assert linkedin.requests == []


def test_a_client_is_built_even_when_unconfigured() -> None:
    from asas_linkedin import LinkedInSettings

    c = LinkedInClient(LinkedInSettings())
    with pytest.raises(LinkedInUnconfiguredError):
        c.sign_in_app()
    with pytest.raises(LinkedInUnconfiguredError):
        c.page_app()


async def test_a_host_supplied_http_client_is_the_hosts_to_close(linkedin: FakeLinkedIn) -> None:
    http = httpx.AsyncClient(transport=httpx.MockTransport(linkedin))
    async with LinkedInClient(settings(), http=http) as c:
        await c.sign_in(code="c")
    assert not http.is_closed
    await http.aclose()


def test_claims_map_both_locale_shapes_and_optional_email() -> None:
    m = member_from_claims({"sub": "s1", "locale": {"country": "AE", "language": "ar"}})
    assert (m.language, m.email, m.email_verified) == ("ar", None, None)
    assert member_from_claims({"sub": "s1", "locale": "en-GB", "email_verified": "false"}).email_verified is False
    with pytest.raises(LinkedInRejectedError):
        member_from_claims({"sub": "  "})


# -- the Page and the Posts API ---------------------------------------------


def page_client(linkedin: FakeLinkedIn) -> LinkedInClient:
    return client(linkedin, page=PAGE, company_id="1337")


async def test_the_page_app_needs_a_company_id_too(linkedin: FakeLinkedIn) -> None:
    with pytest.raises(LinkedInUnconfiguredError):
        client(linkedin, page=PAGE).page_app()
    assert page_client(linkedin).settings.organization_urn == "urn:li:organization:1337"


async def test_the_page_authorization_url_asks_for_the_one_posting_scope(linkedin: FakeLinkedIn) -> None:
    url = await page_client(linkedin).page_authorization_url(state="s")
    query = parse_qs(urlparse(url).query)
    assert query["scope"] == ["w_organization_social"]
    assert query["client_id"] == ["page-app"] and query["redirect_uri"] == [PAGE.redirect_uri]


async def test_create_post_sends_linkedins_versioned_headers(linkedin: FakeLinkedIn) -> None:
    urn = await page_client(linkedin).create_post(access_token="page-token", body={"author": "urn:li:organization:1337"})
    assert urn == "urn:li:share:7380000000000000000"
    request = next(r for r in linkedin.requests if r.url.path == "/rest/posts")
    assert request.headers["LinkedIn-Version"] == "202609"
    assert request.headers["X-Restli-Protocol-Version"] == "2.0.0"
    assert request.headers["Authorization"] == "Bearer page-token"
    assert json.loads(request.content) == {"author": "urn:li:organization:1337"}


@pytest.mark.parametrize(
    "status, body, error",
    [
        (401, {"status": 401, "serviceErrorCode": 65600, "code": "INVALID_ACCESS_TOKEN"}, LinkedInPageNotConnectedError),
        (403, {"status": 403, "serviceErrorCode": 100, "code": "ACCESS_DENIED"}, LinkedInRejectedError),
        (422, {"status": 422, "message": "bad commentary"}, LinkedInRejectedError),
        (429, {"status": 429}, LinkedInUnavailableError),
        (500, {}, LinkedInUnavailableError),
    ],
)
async def test_create_post_failures_map_to_what_the_caller_can_do(
    linkedin: FakeLinkedIn, status: int, body: dict[str, Any], error: type[Exception]
) -> None:
    linkedin.posts_answer = lambda r: httpx.Response(status, json=body)
    with pytest.raises(error):
        await page_client(linkedin).create_post(access_token="t", body={})


async def test_a_created_post_without_an_id_is_unavailable(linkedin: FakeLinkedIn) -> None:
    linkedin.posts_answer = lambda r: httpx.Response(201)
    with pytest.raises(LinkedInUnavailableError):
        await page_client(linkedin).create_post(access_token="t", body={})


async def test_page_grant_reads_refresh_tokens_when_linkedin_issues_them(linkedin: FakeLinkedIn) -> None:
    linkedin.token_answer = lambda r: httpx.Response(200, json={
        "access_token": "page-access", "expires_in": 5184000,
        "refresh_token": "page-refresh", "refresh_token_expires_in": 31536000,
        "scope": "w_organization_social",
    })
    grant = await page_client(linkedin).page_grant(code="c")
    assert (grant.access_token, grant.expires_in, grant.refresh_token, grant.refresh_token_expires_in) == (
        "page-access", 5184000, "page-refresh", 31536000,
    )
    form = parse_qs(next(r for r in linkedin.requests if str(r.url) == TOKEN_URL).content.decode())
    assert form["client_id"] == ["page-app"] and form["redirect_uri"] == [PAGE.redirect_uri]


async def test_a_refresh_sends_the_refresh_grant(linkedin: FakeLinkedIn) -> None:
    linkedin.token_answer = lambda r: httpx.Response(200, json={"access_token": "new", "expires_in": 10})
    grant = await page_client(linkedin).refresh_page_grant(refresh_token="old-refresh")
    assert grant.access_token == "new" and grant.refresh_token is None
    form = parse_qs(next(r for r in linkedin.requests if str(r.url) == TOKEN_URL).content.decode())
    assert form["grant_type"] == ["refresh_token"] and form["refresh_token"] == ["old-refresh"]
