"""asas-cv: what a CV gives up before any model reads it, and the shape after.

Three pure pieces for a product that takes CVs:

- ``text``: bytes (PDF, .docx, plain text) to normalised text, with refusals
  that carry a stable code (unsupported, empty, too large, unreadable, a scan
  with no text layer);
- ``photo``: the person's headshot lifted out of the document, or None, and
  never an exception;
- ``resume``: the parsed CV's sections as Pydantic models, usable as a
  structured-output schema and as the stored shape, with the arithmetic a
  model should not do (durations, total experience) done here.

The model call itself is the host's (``asas-llm``); this package holds no
prompt. Table-less and router-less variant of the Asas host contract: no
session, no ``migrate``, no ``seed``, no ``build_router``, no ``configure``.
"""

from __future__ import annotations

from . import photo, resume, text
from .photo import (
    JPEG_QUALITY,
    MAX_PHOTO_BYTES,
    MAX_PIXELS,
    MIN_SIDE_PX,
    ExtractedPhoto,
    extract_profile_photo,
    looks_like_a_headshot,
)
from .resume import (
    ALL_SECTIONS,
    SECTION_GROUPS,
    AdditionalSections,
    Award,
    CandidateProfile,
    Certification,
    ContactInformation,
    EducationItem,
    EducationSections,
    LanguageItem,
    ParsedCVSections,
    ProfessionalLink,
    ProfileSections,
    Project,
    Publication,
    SkillCategory,
    SkillsSections,
    WorkExperienceEntry,
    WorkExperienceItem,
    WorkExperienceSections,
    format_experience_length,
    normalize_years_of_experience,
    parse_year_month,
    settle_years_of_experience,
    total_experience_months,
)
from .text import (
    CODES,
    DOCX_CONTENT_TYPE,
    MAX_DOCUMENT_BYTES,
    PDF_CONTENT_TYPE,
    TEXT_CONTENT_TYPES,
    ExtractionError,
    cut_on_a_boundary,
    extract_text,
    normalize,
    resolve_content_type,
)

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "ALL_SECTIONS",
    "CODES",
    "DOCX_CONTENT_TYPE",
    "JPEG_QUALITY",
    "MAX_DOCUMENT_BYTES",
    "MAX_PHOTO_BYTES",
    "MAX_PIXELS",
    "MIN_SIDE_PX",
    "PDF_CONTENT_TYPE",
    "SECTION_GROUPS",
    "TEXT_CONTENT_TYPES",
    "AdditionalSections",
    "Award",
    "CandidateProfile",
    "Certification",
    "ContactInformation",
    "EducationItem",
    "EducationSections",
    "ExtractedPhoto",
    "ExtractionError",
    "LanguageItem",
    "ParsedCVSections",
    "ProfessionalLink",
    "ProfileSections",
    "Project",
    "Publication",
    "SkillCategory",
    "SkillsSections",
    "WorkExperienceEntry",
    "WorkExperienceItem",
    "WorkExperienceSections",
    "cut_on_a_boundary",
    "extract_profile_photo",
    "extract_text",
    "format_experience_length",
    "looks_like_a_headshot",
    "normalize",
    "normalize_years_of_experience",
    "parse_year_month",
    "photo",
    "resolve_content_type",
    "resume",
    "settle_years_of_experience",
    "text",
    "total_experience_months",
]
