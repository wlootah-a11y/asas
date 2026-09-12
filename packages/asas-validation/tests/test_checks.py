"""The check library (DR 0004 phase 1): the 44 shipped checks, the two doors,
the derived catalog, enforce, the clock seam, and include_checks."""

import types
from datetime import date, datetime, timedelta

import pytest
from fastapi import HTTPException

from asas_validation import catalog, configure_clock, enforce, include_checks, validate

TODAY = date(2026, 9, 12)


@pytest.fixture(autouse=True)
def _frozen_clock():
    configure_clock(lambda: TODAY)
    yield
    configure_clock(None)


def bad(v):  # a violation, with the field attached where given
    assert v is not None
    return v


# ── dates and time ────────────────────────────────────────────────────────────

def test_temporal_checks():
    d = lambda s: date.fromisoformat(s)
    assert validate.not_in_future(d("2026-09-12")) is None
    assert bad(validate.not_in_future(d("2026-09-13"))).code == "not_in_future"
    assert validate.not_in_past(d("2026-09-12")) is None
    assert bad(validate.not_in_past(d("2026-09-11"))).code == "not_in_past"

    assert validate.after(d("2026-09-14"), d("2026-09-11"), days=3) is None
    v = bad(validate.after(d("2026-09-13"), d("2026-09-11"), days=3))
    assert v.code == "after" and v.params["days"] == 3
    assert validate.before(d("2026-09-08"), d("2026-09-11"), days=3) is None
    assert bad(validate.before(d("2026-09-09"), d("2026-09-11"), days=3)).code == "before"

    assert validate.between(d("2026-09-12"), d("2026-09-01"), d("2026-09-30")) is None
    assert bad(validate.between(d("2026-10-01"), d("2026-09-01"), d("2026-09-30"))).code == "between"

    assert validate.not_older_than(d("2023-09-13"), years=3) is None
    assert bad(validate.not_older_than(d("2023-09-11"), years=3)).code == "not_older_than"
    assert validate.not_beyond(d("2026-09-20"), days=30) is None
    assert bad(validate.not_beyond(d("2026-10-13"), days=30)).code == "not_beyond"
    with pytest.raises(TypeError, match="exactly one"):
        validate.not_older_than(d("2026-01-01"))

    assert validate.age_at_least(d("2008-09-12"), years=18) is None
    assert bad(validate.age_at_least(d("2008-09-13"), years=18)).code == "age_at_least"
    assert validate.age_at_most(d("1962-01-01"), years=65) is None
    assert bad(validate.age_at_most(d("1960-09-11"), years=65)).code == "age_at_most"

    assert validate.on_weekday(d("2026-09-11")) is None            # a Friday
    assert bad(validate.on_weekday(d("2026-09-13"))).code == "on_weekday"  # a Sunday
    assert validate.on_weekday(d("2026-09-13"), allowed=(6,)) is None

    assert validate.within_period(d("2026-09-05"), d("2026-09-10"),
                                  d("2026-09-01"), d("2026-09-30")) is None
    assert bad(validate.within_period(d("2026-08-30"), d("2026-09-10"),
                                      d("2026-09-01"), d("2026-09-30"))).code == "within_period"
    assert validate.no_overlap(d("2026-09-01"), d("2026-09-05"),
                               d("2026-09-06"), d("2026-09-09")) is None
    assert bad(validate.no_overlap(d("2026-09-01"), d("2026-09-07"),
                                   d("2026-09-06"), d("2026-09-09"))).code == "no_overlap"


def test_datetime_meets_date_coerces_instead_of_crashing():
    assert validate.after(datetime(2026, 9, 14, 10, 30), date(2026, 9, 11), days=3) is None
    assert validate.not_in_future(datetime(2026, 9, 12, 23, 59)) is None


def test_clock_is_configurable():
    configure_clock(lambda: date(2030, 1, 1))
    assert validate.not_in_past(date(2029, 12, 31)) is not None


# ── numbers ───────────────────────────────────────────────────────────────────

def test_numeric_checks():
    assert validate.at_least(3, 2) is None
    assert bad(validate.at_least(1, 2)).params["minimum"] == 2
    assert validate.at_most(2, 2) is None
    assert bad(validate.at_most(3, 2)).code == "at_most"
    assert validate.positive(0.1) is None
    assert bad(validate.positive(0)).code == "positive"
    assert validate.non_negative(0) is None
    assert bad(validate.non_negative(-1)).code == "non_negative"
    assert validate.multiple_of(0.15, 0.05) is None
    assert bad(validate.multiple_of(0.16, 0.05)).code == "multiple_of"
    assert validate.max_decimals(12.25) is None
    assert bad(validate.max_decimals(12.256)).code == "max_decimals"
    assert validate.max_decimals(10.0, places=0) is None   # trailing zeros are not decimals
    assert validate.percent(100) is None
    assert bad(validate.percent(101)).code == "percent"
    assert validate.luhn("4539 1488 0343 6467") is None
    assert bad(validate.luhn("4539148803436468")).code == "luhn"
    assert bad(validate.luhn("not-digits")).code == "luhn"


# ── comparing fields ──────────────────────────────────────────────────────────

def test_cross_field_checks():
    assert validate.greater_than(5, 4) is None
    assert bad(validate.greater_than(4, 4)).code == "greater_than"
    assert validate.less_than(3, 4) is None
    assert bad(validate.less_than(4, 4)).code == "less_than"
    assert validate.equal_to("x@y.ae", "x@y.ae") is None
    assert bad(validate.equal_to("a", "b")).code == "equal_to"
    assert validate.different_from("new", "old") is None
    assert bad(validate.different_from("same", "same")).code == "different_from"
    assert validate.sums_to(30, 30, 40, total=100) is None
    assert bad(validate.sums_to(30, 30, total=100)).code == "sums_to"
    assert validate.ratio_at_least(120, 100, ratio=1.2) is None
    assert bad(validate.ratio_at_least(119, 100, ratio=1.2)).code == "ratio_at_least"


# ── presence family: absence is the subject, so None must NOT skip ───────────

def test_presence_checks_fire_on_absent_values():
    assert bad(validate.required_when(None, True)).code == "required_when"
    assert bad(validate.required_when("", True)).code == "required_when"
    assert validate.required_when(None, False) is None
    assert validate.required_when("x", True) is None
    assert bad(validate.forbidden_when("x", True)).code == "forbidden_when"
    assert validate.forbidden_when(None, True) is None
    assert validate.required_together("a", "b") is None
    assert validate.required_together(None, None) is None
    assert bad(validate.required_together("a", None)).code == "required_together"
    assert validate.at_least_one(None, "x") is None
    assert bad(validate.at_least_one(None, "")).code == "at_least_one"
    assert validate.exactly_one(None, "x") is None
    assert bad(validate.exactly_one("a", "b")).code == "exactly_one"


def test_every_non_presence_check_skips_absent_values():
    assert validate.after(None, TODAY) is None
    assert validate.after(TODAY, None) is None
    assert validate.email(None) is None
    assert validate.at_least(None, 2) is None
    assert validate.unique_items(None) is None


# ── formats ───────────────────────────────────────────────────────────────────

def test_format_checks():
    assert validate.email("sara@xdigit.ai") is None
    assert bad(validate.email("nope")).code == "email"
    assert bad(validate.email("sara@other.ai", domains=["xdigit.ai"])).code == "email"
    assert validate.url("https://asas.example") is None
    assert bad(validate.url("ftp://asas.example")).code == "url"
    assert bad(validate.url("https://")).code == "url"
    assert validate.phone("+971 50 123 4567") is None
    assert bad(validate.phone("abc")).code == "phone"
    assert validate.iban("AE07 0331 2345 6789 0123 456") is None
    assert bad(validate.iban("AE07033123456789012345X")).code == "iban"
    assert bad(validate.iban("XX00")).code == "iban"
    assert validate.uuid("6fa459ea-ee8a-3ca4-894e-db77e160355e") is None
    assert bad(validate.uuid("not-a-uuid")).code == "uuid"
    assert validate.slug("asas-validation-2") is None
    assert bad(validate.slug("no spaces!")).code == "slug"
    assert validate.matches("AB-1234", r"[A-Z]{2}-\d{4}") is None
    assert bad(validate.matches("AB1234", r"[A-Z]{2}-\d{4}")).code == "matches"
    assert validate.no_html("plain text") is None
    assert bad(validate.no_html("<b>bold</b>")).code == "no_html"
    assert validate.one_of("open", ["open", "closed"]) is None
    assert bad(validate.one_of("gone", ["open", "closed"])).code == "one_of"
    assert validate.not_in("fine", ["banned"]) is None
    assert bad(validate.not_in("banned", ["banned"])).code == "not_in"


def test_collection_checks():
    assert validate.unique_items([1, 2, 3]) is None
    assert bad(validate.unique_items([1, 2, 2])).code == "unique_items"
    assert validate.subset_of(["a"], ["a", "b"]) is None
    assert bad(validate.subset_of(["a", "z"], ["a", "b"])).params["stray"] == ["z"]
    assert validate.contains_none("a clean sentence", ["banned"]) is None
    assert bad(validate.contains_none("with BANNED word", ["banned"])).code == "contains_none"


# ── the doors, the catalog, enforce, include_checks ──────────────────────────

def test_machine_door_equals_attribute_door():
    a = validate.after(date(2026, 9, 10), date(2026, 9, 11))
    b = validate("after", date(2026, 9, 10), date(2026, 9, 11))
    assert a.code == b.code == "after"


def test_unknown_check_fails_loud_and_names_the_catalog():
    with pytest.raises(LookupError, match="unknown check 'nope'"):
        validate("nope", 1)


def test_catalog_is_derived_from_the_functions():
    cat = {c["name"]: c for c in catalog()}
    assert len(cat) == 44
    after = cat["after"]
    assert after["takes"] == ["value", "reference"]
    assert after["settings"] == {"days": 0}
    assert "must be after" in after["sentence"]
    assert cat["sums_to"]["takes"] == ["parts...", "total"]
    assert all(c["sentence"] for c in cat.values())


def test_include_checks_adds_a_host_module_and_rejects_collisions():
    mod = types.ModuleType("host_checks")
    src = '''
def emirates_id(value, *, field="", message_key=None):
    """{field} must be a valid Emirates ID"""
    from asas_validation.engine import Violation
    if value is None: return None
    if not str(value).startswith("784"):
        return Violation(field, "emirates_id", "Must be a valid Emirates ID")
    return None
'''
    exec(compile(src, "host_checks", "exec"), mod.__dict__)
    mod.emirates_id.__module__ = "host_checks"
    include_checks(mod)
    try:
        assert validate.emirates_id("784-1234") is None
        assert validate.emirates_id("123").code == "emirates_id"
        assert any(c["name"] == "emirates_id" for c in catalog())
        clash = types.ModuleType("clash")
        exec(compile("def after(value, *, field=''):\n    return None", "clash", "exec"),
             clash.__dict__)
        clash.after.__module__ = "clash"
        with pytest.raises(ValueError, match="already exists"):
            include_checks(clash)
    finally:
        from asas_validation import library
        library._MODULES.remove(mod)
        delattr(validate, "emirates_id")


def test_enforce_collects_everything_and_raises_one_422():
    with pytest.raises(HTTPException) as exc:
        enforce([
            validate.email("nope", field="candidate_email"),
            validate.after(date(2026, 9, 9), date(2026, 9, 8), days=3,
                           field="scheduled_date", message_key="interview.min_notice"),
            validate.at_least(3, 2, field="panel_size"),  # passes → dropped
        ])
    detail = exc.value.detail
    assert exc.value.status_code == 422 and len(detail) == 2
    assert detail[0]["loc"] == ["body", "candidate_email"]
    assert detail[1]["message_key"] == "interview.min_notice"
    assert detail[1]["params"]["days"] == 3
    # nothing wrong → enforce is silent
    enforce([None, validate.at_least(3, 2)])
