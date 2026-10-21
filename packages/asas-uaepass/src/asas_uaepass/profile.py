"""What UAE PASS says about a person, parsed and normalised. Pure.

The formats are UAE PASS's own sample responses (Web Application guide,
"3. User Information", and the Attributes List, read 2026-10-06), and the
samples do not agree with each other, so the parser is written for that:

- keys are read without regard to case (``unifiedID`` in a sample,
  ``unifiedId`` in the list);
- ``sub`` varies (``UAEPASS/<uuid>`` or a bare hex string) and is never used:
  the person is ``uuid`` (Standard Implementation Guidelines 4 and 11);
- ``mobile`` arrives with no ``+`` (``971566612311``) and is kept only as E.164;
- full names are positional comma parts (``Ram,,,,ABC,,``) or plain text;
- ``nationalityEN`` is an alpha-3 code from UAE PASS's own list, and ``XXX``
  means nobody knows.

Only a missing ``uuid`` or a ``userType`` outside SOP1/SOP2/SOP3 fails the
sign-in (Web Authentication Implementation Assessment Checklist V1.3: "In case
user type is other than SOP1, SOP2 and SOP3 then appropriate error message
should be displayed"). A bad optional value stores nothing for that field and
is reported by name in ``unparsed``. The Emirates ID number, gender, titles,
``idType`` and ``spuuid`` are never read at all: a service provider keeps
only the attributes its business needs, and those are never among them.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from . import nationalities
from .errors import UaePassRejectedError

USER_TYPES = frozenset({"SOP1", "SOP2", "SOP3"})
"""SOP1: a verified mobile and email. SOP2: verified in person at a kiosk.
SOP3: an Emirates ID holder, verified against it."""

#: UAE PASS attribute -> the identity fact it fills. The only attributes a
#: deployment may keep; ``UaePassSettings.kept_attributes`` chooses among them.
FACT_FOR_ATTRIBUTE: dict[str, str] = {
    "email": "email",
    "mobile": "mobile",
    "firstnameEN": "given_name_en",
    "lastnameEN": "family_name_en",
    "fullnameEN": "full_name_en",
    "firstnameAR": "given_name_ar",
    "lastnameAR": "family_name_ar",
    "fullnameAR": "full_name_ar",
    "nationalityEN": "nationality",
}

_E164 = re.compile(r"^\+[1-9]\d{6,14}$")
_MOBILE_JUNK = re.compile(r"[\s().-]+")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_SPACES = re.compile(r"\s+")
_MAX_NAME = 200
_MAX_EMAIL = 320
_MAX_UUID = 128


@dataclass(frozen=True, slots=True)
class UaePassProfile:
    uuid: str
    user_type: str
    #: Identity facts by fact field (``email``, ``mobile``, ``given_name_en``,
    #: ..., ``nationality``), normalised, only the kept ones, only non-empty.
    facts: Mapping[str, str]
    #: Fact fields UAE PASS sent a value for that could not be used.
    unparsed: tuple[str, ...]
    #: The assurance UAE PASS reports (``acr``), for an audit event only.
    acr: str | None

    @property
    def email(self) -> str | None:
        return self.facts.get("email")

    @property
    def mobile(self) -> str | None:
        return self.facts.get("mobile")


def kept_attributes(setting: str) -> frozenset[str]:
    """A ``kept_attributes`` string as a set, keeping only names this parser
    knows: a typo in the setting keeps less, never more."""
    wanted = {part.strip().lower() for part in setting.split(",") if part.strip()}
    return frozenset(name for name in FACT_FOR_ATTRIBUTE if name.lower() in wanted)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def normalize_mobile(value: Any) -> str | None:
    """``971566612311`` -> ``+971566612311``. None unless the result is E.164."""
    raw = _MOBILE_JUNK.sub("", _text(value))
    if not raw:
        return None
    if raw.startswith("00"):
        raw = raw[2:]
    number = raw if raw.startswith("+") else f"+{raw}"
    return number if _E164.match(number) else None


def normalize_name(value: Any) -> str | None:
    """Trimmed, inner runs of spaces made one. Case is kept as sent: title-
    casing breaks names such as "McDonald" and "Al-Hashimi"."""
    text = _SPACES.sub(" ", _text(value))
    return text[:_MAX_NAME] if text else None


def _name_parts(value: Any) -> list[str]:
    return [part for part in (normalize_name(p) for p in _text(value).split(",")) if part]


def join_name_parts(value: Any) -> str | None:
    """A full name from UAE PASS's positional comma parts, every part kept in
    order. An Arabic full name carries the father's and grandfather's names
    that the first and last name fields drop."""
    joined = " ".join(_name_parts(value))
    return joined[:_MAX_NAME] if joined else None


def normalize_email(value: Any) -> str | None:
    email = _text(value).lower()
    if not email or len(email) > _MAX_EMAIL or not _EMAIL.match(email):
        return None
    return email


def _fact_value(attribute: str, value: Any) -> str | None:
    if attribute == "email":
        return normalize_email(value)
    if attribute == "mobile":
        return normalize_mobile(value)
    if attribute in ("fullnameEN", "fullnameAR"):
        return join_name_parts(value)
    if attribute == "nationalityEN":
        return nationalities.normalize_code(value)
    return normalize_name(value)


def parse_userinfo(body: Any, *, kept: frozenset[str]) -> UaePassProfile:
    """UAE PASS's ``/idshub/userinfo`` body as a :class:`UaePassProfile`.

    ``kept`` is :func:`kept_attributes` of the host's setting. Raises
    :class:`UaePassRejectedError` (``userinfo_shape``, ``no_uuid``,
    ``user_type``) for the three things that fail a sign-in.
    """
    if not isinstance(body, Mapping):
        raise UaePassRejectedError(
            "UAE PASS user information is not an object", reason="userinfo_shape"
        )
    data = {str(key).lower(): value for key, value in body.items()}

    uuid = _text(data.get("uuid"))
    if not uuid or len(uuid) > _MAX_UUID:
        raise UaePassRejectedError("UAE PASS sent no usable uuid", reason="no_uuid")
    user_type = _text(data.get("usertype")).upper()
    if user_type not in USER_TYPES:
        raise UaePassRejectedError("UAE PASS sent an unknown user type", reason="user_type")

    facts: dict[str, str] = {}
    unparsed: list[str] = []
    for attribute, fact in FACT_FOR_ATTRIBUTE.items():
        if attribute not in kept:
            continue
        raw = data.get(attribute.lower())
        if raw is None or _text(raw) == "":
            continue
        value = _fact_value(attribute, raw)
        if value is not None:
            facts[fact] = value
        elif not (attribute == "nationalityEN" and _text(raw).upper() == "XXX"):
            # "XXX" is UAE PASS saying nobody knows, which is not a fault.
            unparsed.append(fact)

    # A first or last name UAE PASS left empty is taken from the full name:
    # its first part and its last, the order the parts arrive in.
    for lang in ("EN", "AR"):
        parts = _name_parts(data.get(f"fullname{lang}".lower()))
        given, family = f"given_name_{lang.lower()}", f"family_name_{lang.lower()}"
        if parts and given not in facts and f"firstname{lang}" in kept:
            facts[given] = parts[0]
        if len(parts) > 1 and family not in facts and f"lastname{lang}" in kept:
            facts[family] = parts[-1]

    acr = _text(data.get("acr")) or None
    return UaePassProfile(
        uuid=uuid,
        user_type=user_type,
        facts=facts,
        unparsed=tuple(sorted(unparsed)),
        acr=acr[:200] if acr else None,
    )
