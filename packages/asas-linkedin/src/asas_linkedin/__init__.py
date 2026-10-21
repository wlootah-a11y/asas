"""asas-linkedin: LinkedIn for a hiring product, the generic half.

Sign in with LinkedIn (OpenID Connect against LinkedIn's live discovery
document and published keys), Apply with LinkedIn (the signed profile
checked and reduced to the fields a product maps), posting as an
organization Page (the token grant, the little-text escaping, the article
post body, the token sealed at rest), and the Jobs XML feed LinkedIn reads
for Job Wrapping. Nothing about any one product: which jobs go in the feed,
where a token row lives and who may connect the Page are the host's.

Table-less and router-less variant of the Asas host contract: no session,
no ``migrate``, no ``seed``, no ``build_router``, no ``configure``.
Everything is an object the host constructs and owns (the ``asas-graph``
shape).
"""

from __future__ import annotations

from . import awli, jobs_feed, little_text, oidc, posts, sealing
from .awli import MAX_PROFILE_BYTES, Verified, keep_mapped_fields, profile_url, verify_profile
from .client import DISCOVERY_CACHE_TTL_S, ID_TOKEN_LEEWAY_S, Discovery, LinkedInClient, TokenGrant
from .errors import (
    LinkedInError,
    LinkedInPageNotConnectedError,
    LinkedInRejectedError,
    LinkedInSignatureInvalidError,
    LinkedInUnavailableError,
    LinkedInUnconfiguredError,
)
from .identity import LinkedInMember, member_from_claims
from .jobs_feed import FeedJob
from .jobs_feed import description as feed_description
from .jobs_feed import problems as feed_problems
from .jobs_feed import render as render_feed
from .little_text import COMMENTARY_MAX, fit_commentary
from .little_text import escape as little_text_escape
from .oidc import JwksKeySet, OidcError, decode_verified
from .posts import ARTICLE_DESCRIPTION_MAX, article_post
from .sealing import load_key, seal, unseal
from .settings import (
    DEFAULT_API_BASE_URL,
    DEFAULT_API_VERSION,
    DEFAULT_DISCOVERY_URL,
    PAGE_SCOPES,
    SIGN_IN_SCOPES,
    AppCredentials,
    LinkedInSettings,
)

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "ARTICLE_DESCRIPTION_MAX",
    "COMMENTARY_MAX",
    "DEFAULT_API_BASE_URL",
    "DEFAULT_API_VERSION",
    "DEFAULT_DISCOVERY_URL",
    "DISCOVERY_CACHE_TTL_S",
    "ID_TOKEN_LEEWAY_S",
    "MAX_PROFILE_BYTES",
    "PAGE_SCOPES",
    "SIGN_IN_SCOPES",
    "AppCredentials",
    "Discovery",
    "FeedJob",
    "JwksKeySet",
    "LinkedInClient",
    "LinkedInError",
    "LinkedInMember",
    "LinkedInPageNotConnectedError",
    "LinkedInRejectedError",
    "LinkedInSettings",
    "LinkedInSignatureInvalidError",
    "LinkedInUnavailableError",
    "LinkedInUnconfiguredError",
    "OidcError",
    "TokenGrant",
    "Verified",
    "article_post",
    "awli",
    "decode_verified",
    "feed_description",
    "feed_problems",
    "fit_commentary",
    "jobs_feed",
    "keep_mapped_fields",
    "little_text",
    "little_text_escape",
    "load_key",
    "member_from_claims",
    "oidc",
    "posts",
    "profile_url",
    "render_feed",
    "seal",
    "sealing",
    "unseal",
    "verify_profile",
]
