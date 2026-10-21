"""UAE PASS user information, parsed and normalised, against UAE PASS's own
sample responses."""

from __future__ import annotations

import pytest

from asas_uaepass import (
    UaePassRejectedError,
    join_name_parts,
    kept_attributes,
    nationalities,
    normalize_mobile,
    parse_userinfo,
)

from .conftest import basic_user, citizen, visitor

ALL = kept_attributes(
    "userType,email,mobile,firstnameEN,lastnameEN,firstnameAR,lastnameAR,"
    "fullnameEN,fullnameAR,nationalityEN"
)


def test_the_citizen_sample_maps_to_facts() -> None:
    profile = parse_userinfo(citizen(), kept=ALL)
    assert profile.uuid == "7a05992e-3244-49d3-bcbc-7894c8fca25e"
    assert profile.user_type == "SOP3"
    assert dict(profile.facts) == {
        "email": "ramabc1234@gmail.com",
        "mobile": "+97151234003",
        "given_name_en": "Ram",
        "family_name_en": "ABC",
        "full_name_en": "Ram ABC",
        "given_name_ar": "ساوميا",
        "family_name_ar": "شارما",
        "full_name_ar": "ساوميا شارما",
        "nationality": "IND",
    }
    assert profile.unparsed == ()
    assert profile.email == "ramabc1234@gmail.com"
    assert profile.mobile == "+97151234003"


def test_the_emirates_id_gender_and_titles_are_never_read() -> None:
    profile = parse_userinfo(
        citizen(titleEN="Dr.", titleAR=".د", idType="ID", spuuid="b1320896"), kept=ALL
    )
    stored = " ".join(profile.facts.values())
    assert "784189014978983" not in stored
    assert "Male" not in stored
    assert "Dr." not in stored
    assert set(profile.facts) <= {
        "email", "mobile", "given_name_en", "family_name_en", "full_name_en",
        "given_name_ar", "family_name_ar", "full_name_ar", "nationality",
    }


def test_the_visitor_sample_keeps_every_part_of_the_arabic_full_name() -> None:
    profile = parse_userinfo(visitor(), kept=ALL)
    assert profile.facts["full_name_ar"] == "عمر بدوى حسنين عبدالله"
    assert profile.facts["full_name_en"] == "AMAR KHAN"
    # Case is kept as UAE PASS sends it, which here is not consistent.
    assert profile.facts["given_name_en"] == "AMAR"
    assert profile.facts["family_name_en"] == "Khan"
    assert profile.facts["email"] == "khan@dubai.ae"
    assert profile.facts["mobile"] == "+971566612311"


def test_an_sop1_account_has_no_nationality_or_arabic_name() -> None:
    profile = parse_userinfo(basic_user(), kept=ALL)
    assert profile.user_type == "SOP1"
    assert "nationality" not in profile.facts
    assert "given_name_ar" not in profile.facts
    assert profile.facts["given_name_en"] == "Fatima"


def test_sub_is_never_the_key() -> None:
    a = parse_userinfo(citizen(sub="UAEPASS/abc"), kept=ALL)
    b = parse_userinfo(citizen(sub="800F475AC0E7A9ED01B2D5D2C25A59B3"), kept=ALL)
    assert a.uuid == b.uuid


def test_keys_are_read_without_regard_to_case() -> None:
    profile = parse_userinfo({"UUID": "u-1", "USERTYPE": "sop2", "EMAIL": "A@B.AE"}, kept=ALL)
    assert profile.uuid == "u-1"
    assert profile.user_type == "SOP2"
    assert profile.facts["email"] == "a@b.ae"


@pytest.mark.parametrize("body", [citizen(uuid=None), citizen(uuid="  ")])
def test_no_uuid_fails_the_sign_in(body: dict[str, object]) -> None:
    with pytest.raises(UaePassRejectedError) as raised:
        parse_userinfo(body, kept=ALL)
    assert raised.value.reason == "no_uuid"


@pytest.mark.parametrize("user_type", [None, "", "SOP4", "visitor"])
def test_an_unknown_user_type_fails_the_sign_in(user_type: str | None) -> None:
    with pytest.raises(UaePassRejectedError) as raised:
        parse_userinfo(citizen(userType=user_type), kept=ALL)
    assert raised.value.reason == "user_type"


def test_not_an_object_fails_the_sign_in() -> None:
    with pytest.raises(UaePassRejectedError) as raised:
        parse_userinfo(["not", "an", "object"], kept=ALL)
    assert raised.value.reason == "userinfo_shape"


def test_xxx_means_nobody_knows_and_is_not_a_fault() -> None:
    profile = parse_userinfo(citizen(nationalityEN="XXX"), kept=ALL)
    assert "nationality" not in profile.facts
    assert profile.unparsed == ()


def test_bad_optional_values_store_nothing_and_are_named() -> None:
    profile = parse_userinfo(
        citizen(nationalityEN="ZZZ", mobile="call me", email="not-an-email"), kept=ALL
    )
    assert {"nationality", "mobile", "email"}.isdisjoint(profile.facts)
    assert profile.unparsed == ("email", "mobile", "nationality")
    # The sign-in itself still goes ahead.
    assert profile.uuid


def test_only_kept_attributes_are_kept() -> None:
    kept = kept_attributes("userType,email,firstnameEN,lastnameEN")
    profile = parse_userinfo(citizen(), kept=kept)
    assert set(profile.facts) == {"email", "given_name_en", "family_name_en"}


def test_a_typo_in_the_setting_keeps_less_never_more() -> None:
    assert kept_attributes("email, idn, gender, nationalityen") == frozenset({"email", "nationalityEN"})


def test_a_missing_first_name_comes_from_the_full_name() -> None:
    profile = parse_userinfo(citizen(firstnameEN=None, lastnameEN=None), kept=ALL)
    assert profile.facts["given_name_en"] == "Ram"
    assert profile.facts["family_name_en"] == "ABC"


def test_the_assurance_level_rides_along_for_the_audit_event() -> None:
    assert parse_userinfo(citizen(acr="urn:safelayer:tws:policies:authentication:level:low"), kept=ALL).acr
    assert parse_userinfo(citizen(), kept=ALL).acr is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("971566612311", "+971566612311"),
        ("+971566612311", "+971566612311"),
        ("00971566612311", "+971566612311"),
        ("971 56 661 2311", "+971566612311"),
        ("9715555555555", "+9715555555555"),
        ("", None),
        ("abc", None),
        ("0", None),
    ],
)
def test_mobile_becomes_e164(raw: str, expected: str | None) -> None:
    assert normalize_mobile(raw) == expected


def test_full_names_join_their_parts_in_order() -> None:
    assert join_name_parts("Ram,,,,ABC,,") == "Ram ABC"
    assert join_name_parts("John Smith") == "John Smith"
    assert join_name_parts(",,,") is None


def test_the_nationality_list_is_uae_pass_s() -> None:
    assert nationalities.UAE == "ARE"
    assert nationalities.name("ARE") == "United Arab Emirates"
    assert nationalities.name("ARE", language="ar") == "الإمارات العربية المتحدة"
    assert nationalities.normalize_code(" are ") == "ARE"
    assert nationalities.normalize_code("XXX") is None
    assert nationalities.normalize_code(None) is None
    assert len(nationalities.NATIONALITIES) == 248
