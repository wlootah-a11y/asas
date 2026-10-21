"""Apply with LinkedIn: check the profile LinkedIn signed, keep only the
fields a host maps, and put them in one shape. Pure.

Sources: "Apply with LinkedIn Plugin" (Microsoft Learn, ms.date 2026-05-05)
and "AWLI Profile Schema" (2022-09-30). The widget calls
``handleProfileData(profileData, signature)``; the schema page says the
signature is "created using raw profile data and developer application's
secret key" and names no algorithm. LinkedIn's sample signature is 64 hex
characters, which fits HMAC-SHA256, and that is what is checked here, with
the app's client secret as the key.

Which exact bytes LinkedIn signs is not documented, so the check tries a
short, fixed list of serialisations of the same data: the string as the
browser sent it first, then the compact JSON forms a browser or LinkedIn
would produce. Every candidate is still an HMAC under the app's OWN secret,
so the list widens nothing for a forger. ``Verified.form`` says which one
matched, and the first real LinkedIn sample settles it.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any

from .errors import LinkedInSignatureInvalidError

#: The longest profile that will even be looked at. LinkedIn's sample is
#: about 2 KB; a long career with every summary filled is well under this.
MAX_PROFILE_BYTES = 256 * 1024

_MAX_ITEMS = {"positions": 50, "educations": 30, "certifications": 50, "languages": 30, "skills": 100}
_TEXT_MAX = 4000
_SHORT_MAX = 300


@dataclass(frozen=True, slots=True)
class Verified:
    profile: dict[str, Any]
    #: Which serialisation the signature matched: "as_sent", "compact",
    #: "compact_ascii" or "sorted".
    form: str
    #: SHA-256 of the string as the browser sent it, kept beside an import
    #: so the stored fields can be tied back to the signed document.
    sha256: str


def _signature_bytes(signature: str) -> bytes | None:
    value = (signature or "").strip()
    if len(value) == 64:
        try:
            return bytes.fromhex(value)
        except ValueError:
            return None
    try:
        decoded = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=False)
    except (binascii.Error, ValueError):
        return None
    return decoded if len(decoded) == 32 else None


def _forms(raw: str, data: Any) -> list[tuple[str, str]]:
    return [
        ("as_sent", raw),
        ("compact", json.dumps(data, separators=(",", ":"), ensure_ascii=False)),
        ("compact_ascii", json.dumps(data, separators=(",", ":"), ensure_ascii=True)),
        ("sorted", json.dumps(data, separators=(",", ":"), ensure_ascii=False, sort_keys=True)),
    ]


def verify_profile(raw: str, signature: str, *, secret: str) -> Verified:
    """The profile, only if LinkedIn's signature checks out under ``secret``.
    Anything else (no secret, no signature, malformed JSON, a mismatch) is
    the same refusal, and nothing from the profile is returned."""
    if not secret or not raw or len(raw.encode()) > MAX_PROFILE_BYTES:
        raise LinkedInSignatureInvalidError("The LinkedIn profile could not be checked")
    expected = _signature_bytes(signature)
    if expected is None:
        raise LinkedInSignatureInvalidError("The LinkedIn profile could not be checked")
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise LinkedInSignatureInvalidError("The LinkedIn profile could not be checked") from exc
    if not isinstance(data, dict):
        raise LinkedInSignatureInvalidError("The LinkedIn profile could not be checked")
    key = secret.encode()
    for form, text in _forms(raw, data):
        digest = hmac.new(key, text.encode(), hashlib.sha256).digest()
        if hmac.compare_digest(digest, expected):
            return Verified(profile=data, form=form, sha256=hashlib.sha256(raw.encode()).hexdigest())
    raise LinkedInSignatureInvalidError("The LinkedIn profile could not be checked")


# ---- keep only what a host maps -------------------------------------------


def _text(value: Any, limit: int = _SHORT_MAX) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned[:limit] if cleaned else None


def _month(value: Any) -> str | None:
    """LinkedIn's ``{"month": 3, "year": 2022}`` or ``{"year": 2017}`` as
    "2022-03" or "2017"."""
    if not isinstance(value, dict):
        return None
    year = value.get("year")
    if not isinstance(year, int) or not 1900 <= year <= 2100:
        return None
    month = value.get("month")
    if isinstance(month, int) and 1 <= month <= 12:
        return f"{year:04d}-{month:02d}"
    return f"{year:04d}"


def _items(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = data.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)][: _MAX_ITEMS[key]]


def profile_url(value: Any) -> str | None:
    """A LinkedIn profile link, or None for anything off linkedin.com, so an
    edited link cannot point somewhere else under the LinkedIn label."""
    url = _text(value, 500)
    if not url:
        return None
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    host = url.split("/", 3)[2].lower() if url.count("/") >= 2 else ""
    if host != "linkedin.com" and not host.endswith(".linkedin.com"):
        return None
    return url


def keep_mapped_fields(profile: dict[str, Any]) -> dict[str, Any]:
    """Only the fields a hiring product maps, in one shape, with lengths
    capped. Everything else LinkedIn sent is dropped here and never stored
    (data minimisation; LinkedIn API Terms 4.4)."""
    raw_location = profile.get("location")
    location: dict[str, Any] = raw_location if isinstance(raw_location, dict) else {}
    positions: list[dict[str, Any]] = []
    for item in _items(profile, "positions"):
        title, company = _text(item.get("title")), _text(item.get("companyName"))
        if not (title or company):
            continue
        is_current = item.get("isCurrent") is True
        positions.append(
            {
                "title": title,
                "company": company,
                "summary": _text(item.get("summary"), _TEXT_MAX),
                "start": _month(item.get("startDate")),
                "end": None if is_current else _month(item.get("endDate")),
                "is_current": is_current,
            }
        )
    educations = [
        {
            "school": _text(item.get("schoolName")),
            "degree": _text(item.get("degree")),
            "field_of_study": _text(item.get("fieldOfStudy")),
            "start": _month(item.get("startDate")),
            "end": _month(item.get("endDate")),
            "activities": _text(item.get("activities"), _TEXT_MAX),
            "notes": _text(item.get("notes"), _TEXT_MAX),
        }
        for item in _items(profile, "educations")
        if _text(item.get("schoolName")) or _text(item.get("degree"))
    ]
    certifications = [
        {
            "name": _text(item.get("name")),
            "authority": _text(item.get("authority")),
            "number": _text(item.get("number") or item.get("licenseNumber"), 120),
            "start": _month(item.get("startDate")),
            "end": _month(item.get("endDate")),
        }
        for item in _items(profile, "certifications")
        if _text(item.get("name"))
    ]
    languages = [
        {"name": _text(item.get("name"), 80), "proficiency": _text(item.get("proficiency"), 40)}
        for item in _items(profile, "languages")
        if _text(item.get("name"), 80)
    ]
    skills = [
        name for name in (_text(item.get("name"), 120) for item in _items(profile, "skills")) if name
    ]
    return {
        "first_name": _text(profile.get("firstName"), 200),
        "last_name": _text(profile.get("lastName"), 200),
        "headline": _text(profile.get("headline"), 500),
        "summary": _text(profile.get("summary"), _TEXT_MAX),
        "email": _text(profile.get("emailAddress"), 320),
        "phone": _text(profile.get("phoneNumber"), 64),
        "location": _text(location.get("locationName"), 200),
        "country": (_text(location.get("country"), 8) or "").lower() or None,
        "profile_url": profile_url(profile.get("publicProfileUrl")),
        "positions": positions,
        "educations": educations,
        "certifications": certifications,
        "languages": languages,
        "skills": skills,
        # Kept only for LinkedIn's own conversion tracking (CONVERSION mode).
        "member_identifier": _text(profile.get("memberIdentifier"), 64),
    }
