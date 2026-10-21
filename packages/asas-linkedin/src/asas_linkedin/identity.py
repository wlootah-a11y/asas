"""Who LinkedIn says signed in, read from a VERIFIED ID token. Pure.

Claims per LinkedIn's "Sign In with LinkedIn using OpenID Connect" (Microsoft
Learn, updated 2024-08-08) and its live discovery document (claims_supported:
iss, aud, iat, exp, sub, name, given_name, family_name, picture, email,
email_verified, locale; read 2026-10-06). ``email`` and ``email_verified``
"may not be included in all responses". ``sub`` is pairwise: unique to the
app that asked.

``picture`` is deliberately not carried: LinkedIn's photo links expire.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import LinkedInRejectedError


@dataclass(frozen=True, slots=True)
class LinkedInMember:
    sub: str
    email: str | None
    #: As LinkedIn states it; None when LinkedIn did not say. A host decides
    #: for itself whether that is enough to skip its own proof of the email.
    email_verified: bool | None
    given_name: str
    family_name: str
    #: Two-letter language from ``locale`` ("en", "ar"), or "" when absent.
    language: str


def _language(locale: Any) -> str:
    """``locale`` arrives as "en_US" in the ID token and as
    {"country": "US", "language": "en"} from userinfo; accept both."""
    if isinstance(locale, dict):
        lang = locale.get("language")
        return str(lang).lower()[:2] if lang else ""
    if isinstance(locale, str) and locale:
        return locale.replace("-", "_").split("_", 1)[0].lower()[:2]
    return ""


def _verified(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    return None


def member_from_claims(claims: dict[str, Any]) -> LinkedInMember:
    """Map verified ID-token claims. A token without ``sub`` is refused: a
    link to an account is keyed on it."""
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub.strip():
        raise LinkedInRejectedError("LinkedIn's ID token has no subject")
    email = claims.get("email")
    return LinkedInMember(
        sub=sub,
        email=email.strip() if isinstance(email, str) and email.strip() else None,
        email_verified=_verified(claims.get("email_verified")),
        given_name=str(claims.get("given_name") or "").strip(),
        family_name=str(claims.get("family_name") or "").strip(),
        language=_language(claims.get("locale")),
    )
