"""The Page share's pure pieces: sealing the Page token, LinkedIn's little
Text Format, and the Posts API request body against LinkedIn's "Posts API"
page (li-lms-2026-09)."""

from __future__ import annotations

import base64
import os

import pytest

from asas_linkedin import (
    COMMENTARY_MAX,
    LinkedInPageNotConnectedError,
    LinkedInUnconfiguredError,
    article_post,
    fit_commentary,
    little_text_escape,
    load_key,
    seal,
    unseal,
)

KEY = base64.b64encode(os.urandom(32)).decode()
CONTEXT = "linkedin_page_connection:tenant-1:access"


def test_a_sealed_token_opens_only_for_its_context() -> None:
    key = load_key(KEY)
    sealed = seal(key, "AQV-page-token", context=CONTEXT)
    assert b"AQV-page-token" not in sealed
    assert unseal(key, sealed, context=CONTEXT) == "AQV-page-token"
    with pytest.raises(LinkedInPageNotConnectedError):
        unseal(key, sealed, context="linkedin_page_connection:tenant-2:access")
    with pytest.raises(LinkedInPageNotConnectedError):
        unseal(key, sealed, context="linkedin_page_connection:tenant-1:refresh")
    other = load_key(base64.b64encode(os.urandom(32)).decode())
    with pytest.raises(LinkedInPageNotConnectedError):
        unseal(other, sealed, context=CONTEXT)


def test_each_seal_uses_a_fresh_nonce() -> None:
    key = load_key(KEY)
    assert seal(key, "same", context=CONTEXT) != seal(key, "same", context=CONTEXT)


def test_a_tampered_row_will_not_open() -> None:
    key = load_key(KEY)
    sealed = bytearray(seal(key, "token", context=CONTEXT))
    sealed[-1] ^= 0x01
    with pytest.raises(LinkedInPageNotConnectedError):
        unseal(key, bytes(sealed), context=CONTEXT)


@pytest.mark.parametrize("bad", ["", "not base64!", base64.b64encode(b"short").decode()])
def test_a_missing_or_wrong_key_is_unconfigured(bad: str) -> None:
    with pytest.raises(LinkedInUnconfiguredError):
        load_key(bad)


def test_little_text_escapes_every_reserved_character() -> None:
    assert little_text_escape("Hiring (Abu Dhabi) #jobs @Org [now] {x} <y> a|b c*d e_f g~h \\") == (
        "Hiring \\(Abu Dhabi\\) \\#jobs \\@Org \\[now\\] \\{x\\} \\<y\\> a\\|b c\\*d e\\_f g\\~h \\\\"
    )
    assert little_text_escape("Plain words, no change.") == "Plain words, no change."


def test_little_text_matches_linkedins_own_escaped_example() -> None:
    """The commentary of "Create Post with Escaped Reserved Characters (Bullet
    List)" in LinkedIn's Community Management Postman collection (Marketing
    Versioned APIs), copied as LinkedIn wrote it."""
    linkedin = "Hello, these are some bullet points:\n\n\\* Point 1\n\\* Point 2\n\\* Point 3"
    assert little_text_escape("Hello, these are some bullet points:\n\n* Point 1\n* Point 2\n* Point 3") == linkedin


def test_the_post_body_is_an_article_post_as_the_page() -> None:
    body = article_post(
        organization_urn="urn:li:organization:1234567",
        title="Senior Data Analyst (Grade 7)",
        summary="x" * 400,
        url="https://www.careers.example/roles/1",
    )
    assert body["author"] == "urn:li:organization:1234567"
    assert body["commentary"] == "Senior Data Analyst \\(Grade 7\\)"
    assert body["visibility"] == "PUBLIC" and body["lifecycleState"] == "PUBLISHED"
    assert body["distribution"] == {
        "feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": [],
    }
    article = body["content"]["article"]
    assert article["source"] == "https://www.careers.example/roles/1"
    assert article["title"] == "Senior Data Analyst (Grade 7)"  # not little text: plain
    assert len(article["description"]) == 300
    assert body["isReshareDisabledByAuthor"] is False


def test_the_callers_own_words_are_used_and_escaped() -> None:
    body = article_post(
        organization_urn="urn:li:organization:1", title="T", summary="", url="https://x",
        commentary="  We're hiring! #Org  ",
    )
    assert body["commentary"] == "We're hiring! \\#Org"


def test_long_words_are_cut_after_escaping_so_the_escapes_count() -> None:
    # 2,000 hashtags escape to 4,000 characters: cut before escaping, this
    # would be over LinkedIn's 3,000.
    fitted = fit_commentary("#" * 2000)
    assert len(fitted) == COMMENTARY_MAX
    assert fitted == "\\#" * (COMMENTARY_MAX // 2)


def test_a_cut_never_leaves_half_an_escape() -> None:
    # An odd prefix puts the limit in the middle of the first escape after it.
    fitted = fit_commentary("a" + "#" * 2000)
    assert len(fitted) == COMMENTARY_MAX - 1
    assert fitted.endswith("\\#")
