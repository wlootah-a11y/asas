"""The parsed CV's shape, and the arithmetic done here rather than by a model."""

from __future__ import annotations

from datetime import date

import pytest

from asas_cv import (
    ALL_SECTIONS,
    SECTION_GROUPS,
    ParsedCVSections,
    ProfessionalLink,
    WorkExperienceEntry,
    WorkExperienceItem,
    format_experience_length,
    normalize_years_of_experience,
    parse_year_month,
    settle_years_of_experience,
    total_experience_months,
)


@pytest.mark.parametrize(
    ("value", "is_end", "expected"),
    [
        ("2022-03", False, date(2022, 3, 1)),
        ("2017", False, date(2017, 1, 1)),
        ("2017", True, date(2017, 12, 1)),
        (" 2019-11 ", True, date(2019, 11, 1)),
        ("March 2022", False, None),
        ("", False, None),
        (None, True, None),
    ],
)
def test_year_month_parsing(value, is_end, expected) -> None:
    assert parse_year_month(value, is_end=is_end) == expected


def test_present_is_today() -> None:
    for word in ("present", "Current", "NOW", "ongoing"):
        assert parse_year_month(word, is_end=True) == date.today()


def test_a_work_entry_computes_its_duration_from_its_dates() -> None:
    entry = WorkExperienceEntry(job_title="Engineer", start_date="2011-01", end_date="2013-02")
    assert entry.duration_months == 25
    assert WorkExperienceEntry(start_date="2016", end_date="2017").duration_months == 23
    assert WorkExperienceEntry(start_date="2020-05", end_date="2019-01").duration_months is None
    assert WorkExperienceEntry(start_date=None, end_date="2019-01").duration_months is None


def test_a_current_role_runs_to_today() -> None:
    today = date.today()
    entry = WorkExperienceEntry(start_date=f"{today.year - 2}-{today.month:02d}", end_date="present")
    assert entry.duration_months == 24


def test_an_entry_with_nothing_said_states_so() -> None:
    assert WorkExperienceItem(job_title="x").responsibilities == ["No enough information provided"]
    assert WorkExperienceItem(job_title="x", achievements=["Shipped it"]).responsibilities == []


def test_total_and_formatted_experience() -> None:
    history = [
        WorkExperienceEntry(start_date="2011-01", end_date="2013-02"),  # 25
        WorkExperienceEntry(start_date="2013-03", end_date="2014-03"),  # 12
        WorkExperienceEntry(start_date="unknown", end_date=None),  # none
    ]
    assert total_experience_months(history) == 37
    assert format_experience_length(history) == "3 years 1 month"
    assert format_experience_length([WorkExperienceEntry(start_date="2020-01", end_date="2020-02")]) == "1 month"
    assert format_experience_length([WorkExperienceEntry(start_date="2019-01", end_date="2021-01")]) == "2 years"
    assert format_experience_length([]) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1 year 8 months", "1.7"),
        ("3 years 1 month", "3.1"),
        ("2 years", "2"),
        ("15+ years", "15"),
        ("over 12.5 years of experience", "12.5"),
        ("8", "8"),
        ("120 years", "80"),
        ("none stated", None),
        ("", None),
        (None, None),
    ],
)
def test_years_of_experience_normalises_to_one_numeric_string(value, expected) -> None:
    assert normalize_years_of_experience(value) == expected


def test_settling_prefers_the_computed_total_over_the_models_statement() -> None:
    sections = ParsedCVSections.model_validate({
        "candidate_profile": {"years_of_experience": "15+ years"},
        "work_experience": [{"start_date": "2011-01", "end_date": "2013-02"}],
    })
    settle_years_of_experience(sections)
    assert sections.candidate_profile.years_of_experience == "2.1"

    undated = ParsedCVSections.model_validate({
        "candidate_profile": {"years_of_experience": "15+ years"},
        "work_experience": [{"job_title": "x"}],
    })
    settle_years_of_experience(undated)
    assert undated.candidate_profile.years_of_experience == "15"


def test_the_section_groups_cover_the_whole_shape_once() -> None:
    assert sorted(ALL_SECTIONS) == sorted(ParsedCVSections.model_fields)
    assert len(ALL_SECTIONS) == len(set(ALL_SECTIONS))
    for _label, sections, model in SECTION_GROUPS:
        assert set(sections) <= set(model.model_fields)


def test_the_shape_is_a_structured_output_schema() -> None:
    schema = ParsedCVSections.model_json_schema()
    assert "work_experience" in schema["properties"]
    assert "ProfessionalLink" in schema["$defs"]
    with pytest.raises(ValueError):
        ProfessionalLink(type="LinkedIn")  # a link needs its url


def test_an_empty_parse_is_a_valid_one() -> None:
    sections = ParsedCVSections()
    assert sections.work_experience == [] and sections.candidate_profile.full_name is None
    assert ParsedCVSections.model_validate(sections.model_dump()) == sections
