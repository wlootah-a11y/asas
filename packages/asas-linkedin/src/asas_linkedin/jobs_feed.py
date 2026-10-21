"""The LinkedIn Jobs XML feed LinkedIn reads for Job Wrapping. Pure.

Shape and rules from LinkedIn's "XML Feeds Development Guide" (Microsoft
Learn, ms.date 2025-11-26, li-lts-2026-04):

- root ``<source>`` with ``lastBuildDate``, ``publisherUrl``, ``publisher``,
  ``expectedJobCount``, then one ``<job>`` per role;
- mandatory per job: ``partnerJobId`` (at most 40 characters), ``title``,
  ``description`` (at least 100 characters, a limited set of HTML tags, in
  CDATA), a location, ``applyUrl``, ``companyId`` (mandatory for an ATS) and
  ``posterEmail``;
- ``city`` and ``country`` take precedence over ``location`` when both are
  given; ``workplaceTypes`` is ``On-site``, ``Hybrid`` or ``Remote``;
  ``jobtype`` is ``FULL_TIME``, ``PART_TIME``, ``CONTRACT``, ``INTERNSHIP``
  or ``VOLUNTEER``; ``listDate`` is MM/DD/YYYY.

Every value is written as CDATA, as LinkedIn's own example does. The
standard library's ElementTree cannot write CDATA, so this writes the
elements itself; the tests parse the output back to prove it is well formed.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from email.utils import format_datetime

PARTNER_JOB_ID_MAX = 40
DESCRIPTION_MIN = 100
WORKPLACE_TYPES = frozenset({"On-site", "Hybrid", "Remote"})
JOB_TYPES = frozenset({"FULL_TIME", "PART_TIME", "CONTRACT", "INTERNSHIP", "VOLUNTEER"})

# XML 1.0 forbids most control characters even inside CDATA.
_ILLEGAL_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


@dataclass(frozen=True, slots=True)
class FeedJob:
    partner_job_id: str
    title: str
    #: Limited HTML (p, ul, li, strong, ...), text already HTML-escaped.
    description_html: str
    #: Characters of readable text in the description, tags excluded.
    description_text_length: int
    apply_url: str
    company_id: str
    poster_email: str
    city: str | None
    country: str | None
    location: str | None
    workplace_type: str | None = None
    job_type: str | None = None
    list_date: date | None = None


def problems(job: FeedJob) -> list[str]:
    """Why LinkedIn would refuse this job, by its own mandatory-field rules.
    Empty means it may go in the feed."""
    found = []
    if not job.partner_job_id or len(job.partner_job_id) > PARTNER_JOB_ID_MAX:
        found.append("partner_job_id")
    if not job.title.strip():
        found.append("title")
    if job.description_text_length < DESCRIPTION_MIN:
        found.append("description_too_short")
    if not job.apply_url.startswith("https://"):
        found.append("apply_url")
    if not job.company_id:
        found.append("company_id")
    if not job.poster_email:
        found.append("poster_email")
    if not (job.location or (job.city and job.country)):
        found.append("location")
    if job.workplace_type is not None and job.workplace_type not in WORKPLACE_TYPES:
        found.append("workplace_type")
    if job.job_type is not None and job.job_type not in JOB_TYPES:
        found.append("job_type")
    return found


def _cdata(value: str) -> str:
    cleaned = _ILLEGAL_XML.sub("", value)
    # "]]>" cannot appear inside CDATA: close the section and open a new one.
    return "<![CDATA[" + cleaned.replace("]]>", "]]]]><![CDATA[>") + "]]>"


def _element(name: str, value: str | None, indent: str) -> str:
    return "" if value is None or value == "" else f"{indent}<{name}>{_cdata(value)}</{name}>\n"


def render(
    jobs: Iterable[FeedJob], *, built_at: datetime, publisher: str, publisher_url: str
) -> bytes:
    """The whole feed. Jobs with ``problems`` must be filtered out first."""
    listed = list(jobs)
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>\n<source>\n',
        _element("lastBuildDate", format_datetime(built_at, usegmt=True), "  "),
        _element("publisherUrl", publisher_url, "  "),
        _element("publisher", publisher, "  "),
        _element("expectedJobCount", str(len(listed)), "  "),
    ]
    for job in listed:
        parts.append("  <job>\n")
        for name, value in (
            ("partnerJobId", job.partner_job_id),
            ("title", job.title),
            ("description", job.description_html),
            ("applyUrl", job.apply_url),
            ("companyId", job.company_id),
            ("location", job.location),
            ("city", job.city),
            ("country", job.country),
            ("workplaceTypes", job.workplace_type),
            ("jobtype", job.job_type),
            ("listDate", job.list_date.strftime("%m/%d/%Y") if job.list_date else None),
            ("posterEmail", job.poster_email),
        ):
            parts.append(_element(name, value, "    "))
        parts.append("  </job>\n")
    parts.append("</source>\n")
    return "".join(parts).encode("utf-8")


def description(sections: Iterable[tuple[str | None, list[str]]]) -> tuple[str, int]:
    """``(html, text length)`` from ``(heading, lines)`` sections: a heading
    as a bold paragraph, one line as a paragraph, several as a list. Only
    tags LinkedIn keeps (p, strong, ul, li). Text is escaped, never trusted."""
    out: list[str] = []
    length = 0
    for heading, lines in sections:
        lines = [line.strip() for line in lines if line and line.strip()]
        if not lines:
            continue
        if heading:
            out.append(f"<p><strong>{html.escape(heading, quote=False)}</strong></p>")
            length += len(heading)
        if len(lines) == 1 and not heading:
            out.append(f"<p>{html.escape(lines[0], quote=False)}</p>")
        else:
            out.append("<ul>" + "".join(f"<li>{html.escape(line, quote=False)}</li>" for line in lines) + "</ul>")
        length += sum(len(line) for line in lines)
    return "".join(out), length
