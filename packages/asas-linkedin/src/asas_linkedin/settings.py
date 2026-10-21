"""What a host must tell the library about its LinkedIn apps.

The library reads no configuration of its own: the host builds a
:class:`LinkedInSettings` from *its* settings object and hands it over.
Settings may be built with an app MISSING, on purpose: a deployment that has
not switched a LinkedIn feature on still constructs the client at boot, and
the calls that need that app fail as :class:`LinkedInUnconfiguredError`
rather than at import.

Two apps, because LinkedIn makes it so: sign-in runs on an app with the
"Sign In with LinkedIn using OpenID Connect" product, and posting as a Page
runs on a SECOND app, since LinkedIn's Community Management API must be the
only product on its app ("new app with no other products", Community
Management App Review).
"""

from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_DISCOVERY_URL = "https://www.linkedin.com/oauth/.well-known/openid-configuration"
DEFAULT_API_BASE_URL = "https://api.linkedin.com"
#: LinkedIn's versioned REST API (the ``LinkedIn-Version`` header). A version
#: lives about a year; LinkedIn announces each sunset.
DEFAULT_API_VERSION = "202609"

#: OpenID Connect scopes for a sign-in: exactly the three the self-serve
#: product grants.
SIGN_IN_SCOPES = ("openid", "profile", "email")
#: The one permission a Page post needs (Posts API: ``w_organization_social``).
PAGE_SCOPES = ("w_organization_social",)


@dataclass(frozen=True)
class AppCredentials:
    """One LinkedIn app: its client id and secret, and the callback registered
    on it (absolute https; LinkedIn ignores query strings on it)."""

    client_id: str = ""
    client_secret: str = field(default="", repr=False)
    redirect_uri: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.redirect_uri)


@dataclass(frozen=True)
class LinkedInSettings:
    """``sign_in`` is the OpenID Connect app; ``page`` the Community
    Management app, which also needs ``company_id`` (the number in
    ``urn:li:organization:{id}``) to post as the Page. ``discovery_url`` is
    where LinkedIn's issuer, endpoints and signing keys are read from at
    runtime, never hard-coded: LinkedIn's live issuer differs from the one
    its own documentation page shows, and the document is the source of
    truth."""

    sign_in: AppCredentials = field(default_factory=AppCredentials)
    page: AppCredentials = field(default_factory=AppCredentials)
    company_id: str = ""
    discovery_url: str = DEFAULT_DISCOVERY_URL
    api_base_url: str = DEFAULT_API_BASE_URL
    api_version: str = DEFAULT_API_VERSION
    timeout_seconds: float = 10.0
    user_agent: str = "asas-linkedin"

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("LinkedInSettings.timeout_seconds must be > 0")
        object.__setattr__(self, "api_base_url", self.api_base_url.rstrip("/"))
        object.__setattr__(self, "company_id", (self.company_id or "").strip())

    @property
    def organization_urn(self) -> str:
        return f"urn:li:organization:{self.company_id}" if self.company_id else ""
