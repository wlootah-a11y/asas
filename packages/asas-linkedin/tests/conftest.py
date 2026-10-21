"""LinkedIn at the HTTP edge, for tests: an ``httpx.MockTransport`` handler
answering with the shapes LinkedIn's live endpoints returned on 2026-10-06
(discovery fields, the token endpoint's errors, the Posts API's 201 +
``x-restli-id``). ID tokens are real RS256 tokens signed with a key minted
here, so the production validator runs unchanged. Never LinkedIn's own keys.
Nothing here touches the network.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Callable
from typing import Any

import httpx
import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from asas_linkedin import AppCredentials, LinkedInClient, LinkedInSettings

ISSUER = "https://www.linkedin.com/oauth"  # LinkedIn's LIVE issuer (discovery, 2026-10-06)
DISCOVERY_URL = "https://www.linkedin.com/oauth/.well-known/openid-configuration"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
JWKS_URL = "https://www.linkedin.com/oauth/openid/jwks"
CLIENT_ID = "dev-client"
CALLBACK = "https://app.example/auth/linkedin/callback"

SIGNING_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
KID = "d929668a-test"

PAGE = AppCredentials(
    client_id="page-app",
    client_secret="page-secret",
    redirect_uri="https://app.example/linkedin/page/connect/callback",
)


def jwk(key: rsa.RSAPrivateKey, kid: str) -> dict[str, Any]:
    entry = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    entry.update({"kid": kid, "alg": "RS256", "use": "sig"})
    return entry


def id_token(**overrides: Any) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": ISSUER, "aud": CLIENT_ID, "iat": now, "exp": now + 3600, "sub": "782bbtaQ",
        "name": "Mariam Al Kaabi", "given_name": "Mariam", "family_name": "Al Kaabi",
        "email": "mariam@example.ae", "email_verified": True, "locale": "en_US",
    }
    key = overrides.pop("_key", SIGNING_KEY)
    kid = overrides.pop("_kid", KID)
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return pyjwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})


def settings(**overrides: Any) -> LinkedInSettings:
    base: dict[str, Any] = dict(
        sign_in=AppCredentials(client_id=CLIENT_ID, client_secret="secret-1", redirect_uri=CALLBACK),
        discovery_url=DISCOVERY_URL,
        timeout_seconds=5.0,
    )
    base.update(overrides)
    return LinkedInSettings(**base)


DISCOVERY = {
    "issuer": ISSUER,
    "authorization_endpoint": "https://www.linkedin.com/oauth/v2/authorization",
    "token_endpoint": TOKEN_URL,
    "userinfo_endpoint": "https://api.linkedin.com/v2/userinfo",
    "jwks_uri": JWKS_URL,
    "response_types_supported": ["code"],
    "subject_types_supported": ["pairwise"],
    "id_token_signing_alg_values_supported": ["RS256"],
    "scopes_supported": ["openid", "profile", "email"],
}


class FakeLinkedIn:
    """Routes httpx requests to canned LinkedIn answers and records them."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.token_answer: Callable[[httpx.Request], httpx.Response] = lambda r: httpx.Response(
            200, json={"access_token": "AQX-token", "expires_in": 5183999, "id_token": id_token(),
                       "scope": "openid,profile,email", "token_type": "Bearer"}
        )
        self.jwks: dict[str, Any] = {"keys": [jwk(SIGNING_KEY, KID)]}
        self.discovery: dict[str, Any] = dict(DISCOVERY)
        self.posts_answer: Callable[[httpx.Request], httpx.Response] = lambda r: httpx.Response(
            201, headers={"x-restli-id": "urn:li:share:7380000000000000000"}
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url).split("?")[0]
        if url == DISCOVERY_URL:
            return httpx.Response(200, json=self.discovery)
        if url == JWKS_URL:
            return httpx.Response(200, json=self.jwks)
        if url == TOKEN_URL:
            return self.token_answer(request)
        if url == "https://api.linkedin.com/rest/posts":
            return self.posts_answer(request)
        return httpx.Response(404)


def client(fake: FakeLinkedIn, **overrides: Any) -> LinkedInClient:
    return LinkedInClient(settings(**overrides), transport=httpx.MockTransport(fake))


@pytest.fixture
def linkedin() -> FakeLinkedIn:
    return FakeLinkedIn()


#: The sample response on LinkedIn's "Apply with LinkedIn Plugin" page
#: (Microsoft Learn, li-lts-2026-04), verbatim: the shape the AWLI widget
#: hands ``handleProfileData``.
AWLI_DOC_SAMPLE: dict[str, Any] = {
    "firstName": "FirstName",
    "lastName": "LastName",
    "headline": "Software engineer at LinkedIn Corporation",
    "summary": "This is my Summary.",
    "location": {"locationName": "San Francisco Bay Area", "country": "us"},
    "publicProfileUrl": "www.linkedin.com/in/firstname-lastname-123",
    "positions": [
        {"title": "Software Engineer", "summary": "Software development", "startDate": None,
         "endDate": None, "isCurrent": True, "companyName": "LinkedIn Corporation",
         "company": {"name": "LinkedIn", "type": "EDUCATIONAL"}},
        {"title": "abc", "summary": None, "startDate": {"month": 1, "year": 2011},
         "endDate": {"month": 2, "year": 2013}, "isCurrent": False,
         "companyName": "nonexistingcompany", "company": None},
    ],
    "skills": [],
    "educations": [
        {"schoolName": "University of Pennsylvania", "fieldOfStudy": "Computer Science",
         "startDate": {"year": 2016}, "endDate": {"year": 2020},
         "degree": "Master of Engineering (MEng)",
         "activities": "a brief description to my UPenn activities", "notes": "Hanging around"},
    ],
    "languages": [{"name": "English", "proficiency": "ELEMENTARY"},
                  {"name": "Chinese (Simplified)", "proficiency": "PROFESSIONAL_WORKING"}],
    "certifications": [{"name": "Certified Java Programmer", "authority": "Coursera",
                        "number": "1233211234-4", "startDate": {"month": 1, "year": 2000},
                        "endDate": {"month": 1, "year": 2000}}],
    "emailAddress": "testuser@linkedin.com",
    "phoneNumber": "123-321-1234",
    "memberIdentifier": "VuxQHF3ULX",
}


def awli_signed(profile: dict[str, Any], secret: str) -> tuple[str, str]:
    """``(raw JSON, signature)`` as the widget would hand them over: HMAC-SHA256
    over the compact JSON with the app secret."""
    raw = json.dumps(profile, separators=(",", ":"), ensure_ascii=False)
    return raw, hmac.new(secret.encode(), raw.encode(), hashlib.sha256).hexdigest()
