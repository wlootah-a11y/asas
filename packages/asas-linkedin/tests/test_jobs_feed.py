"""The LinkedIn Jobs XML writer against LinkedIn's "XML Feeds Development
Guide" (ms.date 2025-11-26)."""

from __future__ import annotations

import dataclasses
import xml.etree.ElementTree as ET
from datetime import UTC, date, datetime

import pytest

from asas_linkedin import FeedJob, feed_description, feed_problems, render_feed

BUILT = datetime(2026, 10, 6, 9, 30, tzinfo=UTC)


def _job(**overrides) -> FeedJob:
    html, length = feed_description(
        [
            (None, ["We are hiring a senior data analyst for public service performance work."]),
            ("Responsibilities", ["Own the dashboards", "Train analysts & reviewers"]),
        ]
    )
    base = dict(
        partner_job_id="0f8c3e0e-4c52-4f1a-9a39-0d0b6d1e2f3a",
        title="Senior Data Analyst",
        description_html=html,
        description_text_length=length,
        apply_url="https://www.careers.example/roles/0f8c3e0e-4c52-4f1a-9a39-0d0b6d1e2f3a",
        company_id="1234567",
        poster_email="recruitment@example.gov",
        city="Abu Dhabi",
        country="AE",
        location="Abu Dhabi, United Arab Emirates",
        workplace_type="Hybrid",
        job_type="FULL_TIME",
    )
    base.update(overrides)
    return FeedJob(**base)


def test_the_feed_has_linkedins_shape_and_every_mandatory_field() -> None:
    body = render_feed([_job()], built_at=BUILT, publisher="Example", publisher_url="https://www.careers.example")
    root = ET.fromstring(body)
    assert root.tag == "source"
    assert root.findtext("lastBuildDate") == "Tue, 06 Oct 2026 09:30:00 GMT"
    assert root.findtext("publisherUrl") == "https://www.careers.example"
    assert root.findtext("expectedJobCount") == "1"
    [job] = root.findall("job")
    for name in ("partnerJobId", "title", "description", "applyUrl", "companyId", "posterEmail"):
        assert job.findtext(name), name
    assert (job.findtext("city"), job.findtext("country")) == ("Abu Dhabi", "AE")
    assert job.findtext("workplaceTypes") == "Hybrid" and job.findtext("jobtype") == "FULL_TIME"
    assert b"<![CDATA[" in body


def test_the_description_is_html_in_cdata_with_the_text_escaped() -> None:
    body = render_feed([_job()], built_at=BUILT, publisher="", publisher_url="https://x")
    text = ET.fromstring(body).find("job").findtext("description")
    assert "<p><strong>Responsibilities</strong></p>" in text
    assert "<li>Train analysts &amp; reviewers</li>" in text


def test_cdata_terminators_and_control_characters_cannot_break_the_document() -> None:
    body = render_feed(
        [_job(title="Analyst ]]> <evil/>\x01")], built_at=BUILT, publisher="", publisher_url="https://x"
    )
    assert ET.fromstring(body).find("job").findtext("title") == "Analyst ]]> <evil/>"


def test_empty_optional_fields_are_left_out_and_the_list_date_is_american() -> None:
    body = render_feed(
        [_job(workplace_type=None, job_type=None, location=None, list_date=date(2026, 10, 6))],
        built_at=BUILT, publisher="", publisher_url="https://x",
    )
    job = ET.fromstring(body).find("job")
    assert job.find("workplaceTypes") is None and job.find("jobtype") is None
    assert job.findtext("listDate") == "10/06/2026"
    assert ET.fromstring(body).find("publisher") is None


def test_a_job_that_meets_the_rules_has_no_problems() -> None:
    assert feed_problems(_job()) == []


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"partner_job_id": "x" * 41}, "partner_job_id"),
        ({"title": " "}, "title"),
        ({"description_text_length": 99}, "description_too_short"),
        ({"apply_url": "http://www.careers.example/roles/1"}, "apply_url"),
        ({"company_id": ""}, "company_id"),
        ({"poster_email": ""}, "poster_email"),
        ({"city": None, "country": None, "location": None}, "location"),
        ({"workplace_type": "Onsite"}, "workplace_type"),
        ({"job_type": "SECONDMENT"}, "job_type"),
    ],
)
def test_each_mandatory_rule_is_checked(change: dict, problem: str) -> None:
    assert problem in feed_problems(dataclasses.replace(_job(), **change))


def test_the_description_length_counts_text_not_tags() -> None:
    html, length = feed_description([("Benefits", ["Housing"]), (None, ["  "])])
    assert html == "<p><strong>Benefits</strong></p><ul><li>Housing</li></ul>"
    assert length == len("Benefits") + len("Housing")
