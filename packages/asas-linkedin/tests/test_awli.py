"""Apply with LinkedIn's signature check and field allow-list, on LinkedIn's
own documented sample."""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json

import pytest

from asas_linkedin import LinkedInSignatureInvalidError, keep_mapped_fields, verify_profile

from .conftest import AWLI_DOC_SAMPLE, awli_signed

SECRET = "app-secret-1"


def _sign(text: str, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), text.encode(), hashlib.sha256).hexdigest()


def test_linkedins_sample_verifies_as_sent() -> None:
    raw, signature = awli_signed(AWLI_DOC_SAMPLE, SECRET)
    verified = verify_profile(raw, signature, secret=SECRET)
    assert verified.profile == AWLI_DOC_SAMPLE
    assert verified.form == "as_sent"
    assert verified.sha256 == hashlib.sha256(raw.encode()).hexdigest()


def test_a_signature_over_the_compact_form_verifies_when_the_browser_re_serialised() -> None:
    compact = json.dumps(AWLI_DOC_SAMPLE, separators=(",", ":"), ensure_ascii=False)
    pretty = json.dumps(AWLI_DOC_SAMPLE, indent=2)
    assert verify_profile(pretty, _sign(compact), secret=SECRET).form == "compact"


def test_a_base64_signature_is_read_too() -> None:
    raw, _ = awli_signed(AWLI_DOC_SAMPLE, SECRET)
    digest = hmac.new(SECRET.encode(), raw.encode(), hashlib.sha256).digest()
    assert verify_profile(raw, base64.b64encode(digest).decode(), secret=SECRET).form == "as_sent"


@pytest.mark.parametrize(
    "tamper",
    [
        lambda p: p.update(headline="Chief Executive"),
        lambda p: p["positions"][0].update(title="Director"),
        lambda p: p.update(emailAddress="someone.else@example.com"),
    ],
)
def test_a_tampered_profile_is_refused(tamper) -> None:
    _, signature = awli_signed(AWLI_DOC_SAMPLE, SECRET)
    changed = copy.deepcopy(AWLI_DOC_SAMPLE)
    tamper(changed)
    with pytest.raises(LinkedInSignatureInvalidError):
        verify_profile(json.dumps(changed, separators=(",", ":")), signature, secret=SECRET)


@pytest.mark.parametrize(
    ("raw", "signature", "secret"),
    [
        ("{}", _sign("{}", "other-secret"), SECRET),
        ("{}", _sign("{}"), ""),
        ("{}", "", SECRET),
        ("{}", "not-hex-and-not-base64!", SECRET),
        ("[1, 2]", _sign("[1, 2]"), SECRET),
        ("not json", _sign("not json"), SECRET),
        ("", _sign(""), SECRET),
    ],
)
def test_everything_else_is_the_same_refusal(raw: str, signature: str, secret: str) -> None:
    with pytest.raises(LinkedInSignatureInvalidError) as caught:
        verify_profile(raw, signature, secret=secret)
    assert caught.value.code == "linkedin_signature_invalid"
    assert caught.value.http_status == 400


def test_only_the_mapped_fields_are_kept_in_one_shape() -> None:
    kept = keep_mapped_fields(AWLI_DOC_SAMPLE)
    assert kept["first_name"] == "FirstName" and kept["last_name"] == "LastName"
    assert kept["email"] == "testuser@linkedin.com"
    assert kept["phone"] == "123-321-1234"
    assert kept["location"] == "San Francisco Bay Area" and kept["country"] == "us"
    assert kept["profile_url"] == "https://www.linkedin.com/in/firstname-lastname-123"
    assert kept["positions"][0] == {
        "title": "Software Engineer", "company": "LinkedIn Corporation",
        "summary": "Software development", "start": None, "end": None, "is_current": True,
    }
    assert kept["positions"][1]["start"] == "2011-01" and kept["positions"][1]["end"] == "2013-02"
    assert kept["educations"] == [{
        "school": "University of Pennsylvania", "degree": "Master of Engineering (MEng)",
        "field_of_study": "Computer Science", "start": "2016", "end": "2020",
        "activities": "a brief description to my UPenn activities", "notes": "Hanging around",
    }]
    assert kept["certifications"][0]["number"] == "1233211234-4"
    assert kept["languages"][1] == {"name": "Chinese (Simplified)", "proficiency": "PROFESSIONAL_WORKING"}
    assert kept["skills"] == []
    assert kept["member_identifier"] == "VuxQHF3ULX"
    # What is not mapped is not carried: the company object.
    assert "EDUCATIONAL" not in json.dumps(kept)


def test_a_profile_link_off_linkedin_is_dropped() -> None:
    profile = dict(AWLI_DOC_SAMPLE, publicProfileUrl="https://evil.example/in/x")
    assert keep_mapped_fields(profile)["profile_url"] is None


def test_odd_shapes_do_not_break_the_allow_list() -> None:
    kept = keep_mapped_fields(
        {"positions": "nope", "educations": [None, 3, {"schoolName": "  "}], "location": "x",
         "skills": [{"name": "SQL"}, {"name": ""}, "Python"], "firstName": 7}
    )
    assert kept["positions"] == [] and kept["educations"] == []
    assert kept["skills"] == ["SQL"] and kept["first_name"] is None and kept["location"] is None
