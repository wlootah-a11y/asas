"""The shape of a parsed CV, and the arithmetic a model should not do.

A hiring product asks a model to turn a CV's text into sections (profile,
contact, work history, education, certifications, skills, projects,
languages, awards, publications, links). The models here are that shape,
usable as a structured-output schema by any model call (``asas-llm``'s
``response_format`` takes a Pydantic model) and as the stored shape a host
reads back. Field descriptions are written for the model as well as the
reader, which is why some are long.

What is deterministic is kept out of the model's hands:

- a work entry's ``duration_months`` is computed from its dates
  (``YYYY-MM``, ``YYYY``, or ``present``), never asked for;
- the total experience is the sum of those durations
  (``format_experience_length``), with the model's own "15+ years" as the
  fallback, and both forms normalise to one numeric string
  (``normalize_years_of_experience``: "15", "1.7", capped at 80).
"""

from __future__ import annotations

import re
from datetime import date

from pydantic import BaseModel, Field, model_validator

PRESENT_WORDS = frozenset({"present", "current", "now", "ongoing"})
MAX_YEARS_OF_EXPERIENCE = 80.0


def parse_year_month(value: str | None, *, is_end: bool = False) -> date | None:
    """``YYYY-MM`` or ``YYYY`` as a date on the first of the month; a word for
    "present" as today. A year alone is January for a start and December for
    an end, so a career stated in years is not shortened by a year."""
    if not value:
        return None
    v = value.strip().lower()
    if v in PRESENT_WORDS:
        return date.today()
    try:
        year, month = v.split("-")
        return date(int(year), int(month), 1)
    except (ValueError, AttributeError):
        pass
    try:
        return date(int(v), 12 if is_end else 1, 1)
    except (ValueError, AttributeError):
        return None


class CandidateProfile(BaseModel):
    full_name: str | None = None
    nationality: str | None = None
    current_title: str | None = Field(
        default=None,
        description="Current/most-recent job title; derive from the latest role if not stated explicitly. Can be a combination of multiple titles if the candidate has multiple concurrent roles.",
    )
    current_company: str | None = Field(
        default=None,
        description="Current/most-recent employer; derive from the latest role (the one ending in 'present') if not stated explicitly. Can be a combination of multiple companies if the candidate has multiple concurrent roles.",
    )
    location: str | None = None
    #: The model extracts the stated experience ("15+ years"); a host then
    #: overwrites it with the computed total (``format_experience_length``)
    #: and normalises both forms (``normalize_years_of_experience``).
    years_of_experience: str | None = None
    seniority_level: str | None = None
    industry: str | None = None


class ContactInformation(BaseModel):
    email: str | None = None
    phone: str | None = None
    address: str | None = None
    city: str | None = None
    country: str | None = None


class WorkExperienceItem(BaseModel):
    job_title: str | None = None
    company: str | None = None
    location: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    employment_type: str | None = None
    responsibilities: list[str] = Field(default_factory=list)
    achievements: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _fill_empty_details(self) -> WorkExperienceItem:
        if not self.responsibilities and not self.achievements:
            self.responsibilities = ["No enough information provided"]
        return self


class WorkExperienceEntry(WorkExperienceItem):
    """A work item enriched with a deterministically computed duration."""

    duration_months: int | None = None

    @model_validator(mode="after")
    def _compute_duration(self) -> WorkExperienceEntry:
        start = parse_year_month(self.start_date, is_end=False)
        end = parse_year_month(self.end_date, is_end=True)
        if start and end and end >= start:
            self.duration_months = (end.year - start.year) * 12 + (end.month - start.month)
        else:
            self.duration_months = None
        return self


class EducationItem(BaseModel):
    degree: str | None = None
    field_of_study: str | None = None
    institution: str | None = None
    location: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    grade_or_gpa: str | None = None


class Certification(BaseModel):
    name: str | None = None
    issuer: str | None = None
    issue_date: str | None = None
    expiry_date: str | None = None
    credential_id: str | None = None
    credential_url: str | None = None


class SkillCategory(BaseModel):
    """A profession-agnostic group of skills; ``category`` is named by the
    model to fit the person's field ("Programming Languages", "Clinical
    Skills", "Culinary Techniques")."""

    category: str | None = None
    items: list[str] = Field(default_factory=list)


class Project(BaseModel):
    name: str | None = None
    description: str | None = None
    role: str | None = None
    technologies: list[str] = Field(default_factory=list)
    start_date: str | None = None
    end_date: str | None = None
    url: str | None = None


class LanguageItem(BaseModel):
    """A SPOKEN language the person knows. Not a programming, markup or query
    language: those belong to ``skills``."""

    language: str | None = Field(
        default=None, description="The spoken/human language name, e.g. 'English', 'Arabic'."
    )
    proficiency: str | None = Field(
        default=None,
        description=(
            "Proficiency as stated in the CV, e.g. 'Native', 'Fluent', 'B2', "
            "'Conversational'. Null if the CV does not state one."
        ),
    )


class Award(BaseModel):
    title: str | None = None
    issuer: str | None = None
    date: str | None = None
    description: str | None = None


class Publication(BaseModel):
    title: str | None = None
    publisher: str | None = None
    date: str | None = None
    url: str | None = None


class ProfessionalLink(BaseModel):
    type: str = Field(
        description="The platform or kind of link, e.g. 'LinkedIn', 'GitHub', 'Portfolio', 'Website'.",
    )
    url: str = Field(
        description=(
            "The full web address of the link (e.g. 'https://www.linkedin.com/in/...'). "
            "This must be an actual URL/address present in the CV, not just a label. "
            "If the CV only mentions a platform name (e.g. 'LinkedIn') without any "
            "address or handle, do NOT emit the link at all; omit the whole entry "
            "rather than repeating the label as the url."
        ),
    )


# --- Per-group output models (one per extraction call, when a host runs the
# groups in parallel; the section tuples never overlap) ---


class ProfileSections(BaseModel):
    candidate_profile: CandidateProfile
    contact_information: ContactInformation
    professional_summary: str | None = Field(
        default=None,
        description=(
            "The candidate's own professional summary exactly as written in the CV, "
            "if present; otherwise null. Do not generate one."
        ),
    )
    ai_summary: str = Field(
        description=(
            "A concise AI-generated summary of the candidate synthesized from the "
            "entire CV. This is generated, not copied from the CV. Must be no more "
            "than 3 sentences."
        ),
    )


class WorkExperienceSections(BaseModel):
    work_experience: list[WorkExperienceItem] = Field(default_factory=list)


class EducationSections(BaseModel):
    education: list[EducationItem] = Field(default_factory=list)
    certifications: list[Certification] = Field(default_factory=list)


class SkillsSections(BaseModel):
    skills: list[SkillCategory] = Field(default_factory=list)


class AdditionalSections(BaseModel):
    projects: list[Project] = Field(default_factory=list)
    languages: list[LanguageItem] = Field(default_factory=list)
    awards_and_achievements: list[Award] = Field(default_factory=list)
    publications: list[Publication] = Field(default_factory=list)
    professional_links: list[ProfessionalLink] = Field(
        default_factory=list,
        description=(
            "The CANDIDATE'S OWN professional/social links only (e.g. their LinkedIn, "
            "GitHub, portfolio, personal website). Do NOT include links belonging to "
            "other people; in particular, ignore any URLs found in a References, "
            "Referees, or Recommendations section, as those belong to the referees, "
            "not the candidate. Only include an entry when it has an actual URL."
        ),
    )


#: ``(label, sections, output model)`` per extraction call. Together the
#: tuples cover every field of :class:`ParsedCVSections` and never overlap.
SECTION_GROUPS: tuple[tuple[str, tuple[str, ...], type[BaseModel]], ...] = (
    ("profile", ("candidate_profile", "contact_information", "professional_summary"), ProfileSections),
    ("work-experience", ("work_experience",), WorkExperienceSections),
    ("education", ("education", "certifications"), EducationSections),
    ("skills", ("skills",), SkillsSections),
    (
        "additional",
        ("projects", "languages", "awards_and_achievements", "publications", "professional_links"),
        AdditionalSections,
    ),
)

#: Every section name across all groups, in document order.
ALL_SECTIONS: tuple[str, ...] = tuple(s for _, sections, _ in SECTION_GROUPS for s in sections)


class ParsedCVSections(BaseModel):
    """The whole parsed CV."""

    candidate_profile: CandidateProfile = Field(default_factory=CandidateProfile)
    contact_information: ContactInformation = Field(default_factory=ContactInformation)
    professional_summary: str | None = None
    work_experience: list[WorkExperienceEntry] = Field(default_factory=list)
    education: list[EducationItem] = Field(default_factory=list)
    certifications: list[Certification] = Field(default_factory=list)
    skills: list[SkillCategory] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    languages: list[LanguageItem] = Field(default_factory=list)
    awards_and_achievements: list[Award] = Field(default_factory=list)
    publications: list[Publication] = Field(default_factory=list)
    professional_links: list[ProfessionalLink] = Field(default_factory=list)


# --- the arithmetic -----------------------------------------------------------


def total_experience_months(work_experience: list[WorkExperienceEntry]) -> int:
    """The sum of every entry's computed duration; overlapping roles count
    twice, as a CV states them."""
    return sum(w.duration_months for w in work_experience if w.duration_months)


def format_experience_length(work_experience: list[WorkExperienceEntry]) -> str | None:
    """``"<years> years <months> months"`` from the durations, None when no
    entry carries one."""
    months = total_experience_months(work_experience)
    if not months:
        return None
    years, rem = divmod(months, 12)
    parts: list[str] = []
    if years:
        parts.append(f"{years} year{'s' if years != 1 else ''}")
    if rem:
        parts.append(f"{rem} month{'s' if rem != 1 else ''}")
    return " ".join(parts)


def normalize_years_of_experience(value: str | None) -> str | None:
    """A years-of-experience value as ONE numeric string: a non-negative number
    from 0 to 80 with at most one decimal ("0", "8", "15", "1.7", "12.5").

    Reads the computed form ("1 year 8 months") and the model's free text
    ("15+ years", "over a decade" yields None). None when no number is in it.
    """
    if not value:
        return None
    text = value.lower()
    year_match = re.search(r"(\d+(?:\.\d+)?)\s*year", text)
    month_match = re.search(r"(\d+(?:\.\d+)?)\s*month", text)
    if year_match or month_match:
        total = (float(year_match.group(1)) if year_match else 0.0) + (
            float(month_match.group(1)) if month_match else 0.0
        ) / 12
    else:
        number = re.search(r"\d+(?:\.\d+)?", text)
        if number is None:
            return None
        total = float(number.group())
    total = max(0.0, min(MAX_YEARS_OF_EXPERIENCE, round(total, 1)))
    return str(int(total)) if total.is_integer() else f"{total:.1f}"


def settle_years_of_experience(sections: ParsedCVSections) -> ParsedCVSections:
    """The profile's ``years_of_experience`` as the computed total where the
    work history carries dates, else the model's own statement, either way
    normalised. Returns the same object, changed in place."""
    computed = format_experience_length(sections.work_experience)
    stated = sections.candidate_profile.years_of_experience
    sections.candidate_profile.years_of_experience = normalize_years_of_experience(computed or stated)
    return sections
