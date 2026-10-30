# asas-cv

What a CV gives up before any model reads it, and the shape after. Three
pure pieces for a product that takes CVs:

- **`text`**: bytes (PDF, Word `.docx`, plain text or Markdown) to normalised
  text, with refusals that carry a stable code;
- **`photo`**: the person's headshot lifted out of the document, or `None`,
  and never an exception;
- **`resume`**: the parsed CV's sections as Pydantic models, usable as a
  structured-output schema for the model call and as the stored shape, with
  the arithmetic a model should not do (durations, total experience) done
  here.

The model call itself is the host's (`asas-llm`'s `response_format` takes
`ParsedCVSections` or any of its section groups); this package holds no
prompt. Table-less and router-less variant of the Asas host contract: no
session, no `migrate`, no `seed`, no `build_router`, no `configure`.
Dependencies: pdfminer.six, python-docx, Pillow, pydantic.

## Quickstart

```
asas-cv @ git+https://github.com/wlootah-a11y/asas.git@asas-cv/v0.1.0#subdirectory=packages/asas-cv
```

```python
import asas_cv

# 1. the text a model will read
try:
    text = asas_cv.extract_text(raw, filename=upload.filename, declared_type=upload.content_type)
except asas_cv.ExtractionError as refused:
    refused.code  # document_unsupported_type | document_empty | document_too_large
                  # | document_unreadable | document_no_text

# 2. the headshot, if the CV carries one (never raises)
photo = asas_cv.extract_profile_photo(raw, upload.filename)
if photo:
    store(photo.content, photo.mime_type)   # already a JPEG or PNG

# 3. the shape the model fills, and the arithmetic done after
sections = asas_cv.ParsedCVSections.model_validate(model_output)
asas_cv.settle_years_of_experience(sections)   # computed from the dated roles, else the model's own words
```

## What it gets right, so you do not have to

- **Trust the extension, not the browser's content type.** Windows sends
  `application/octet-stream` for a `.docx` often enough that trusting it
  would refuse the single most common document format.
- **A Word document's substance sits in TABLES**, which python-docx keeps out
  of `paragraphs` entirely, and its paragraphs and tables are two separate
  sequences, so reading one then the other moves every table to the end of
  the file. The body XML is walked in document order instead, and the walk
  falls back rather than failing if those internals ever move.
- **A scan is a real document with no text in it.** The remedy (type the
  text) is not the remedy for a corrupt upload, so `document_no_text` is its
  own code. The size cap is checked before the parse, so an eleven-megabyte
  PDF is refused rather than parsed and then refused.
- **Text files: UTF-16 only on a BOM** (without one it decodes almost any
  bytes into garbage instead of raising), and cp1256 before latin-1, because
  Arabic text saved from Windows Notepad is a realistic input and latin-1
  decodes anything, so it must be last.
- **The headshot reader is pdfminer, not a hand-written PDF scanner.** A
  hand-written one has three faults a real parser has solved: `/Filter` is a
  chain (ReportLab writes `[/ASCII85Decode /FlateDecode]`), `/DecodeParms`
  can carry a predictor (Word does; raw samples come out sheared), and an
  object is reached through the cross-reference table. The first version
  with those faults passed its own tests and found nothing in any real CV.
- **Which image is the face:** big enough, not a page scan, portrait to
  mildly wide; earliest page first, then the largest. A letterhead banner, a
  logo and a full-page scan all lose. A JPEG or PNG is passed through
  untouched; raw samples are encoded as a JPEG (174 times cheaper than PNG on
  a real headshot, for a sixth of the bytes, and the photograph was a JPEG
  before the producer expanded it). CMYK is skipped rather than guessed: a
  wrong face is worse than no face.
- **Durations are computed, not asked for.** Each work entry's
  `duration_months` comes from its `YYYY-MM` or `YYYY` dates (a year alone is
  January for a start and December for an end, so a career stated in years
  is not shortened); `present` runs to today. The total is the sum, and
  `normalize_years_of_experience` turns both the computed form ("1 year 8
  months") and the model's free text ("15+ years") into one numeric string,
  capped at 80.
- **The section groups never overlap** (pinned by a test), so a host that
  runs five parallel extraction calls, one per group, can merge them into one
  `ParsedCVSections` with nothing written twice.

## Surface

- `extract_text(data, *, filename, declared_type=None, max_bytes=10 MB)`,
  `resolve_content_type`, `cut_on_a_boundary(text, limit)`, `normalize`,
  `ExtractionError` (`.code` in `CODES`), `MAX_DOCUMENT_BYTES`.
- `extract_profile_photo(raw, file_name) -> ExtractedPhoto | None`
  (`content`, `mime_type`, `width`, `height`), `looks_like_a_headshot`,
  the size constants.
- `ParsedCVSections` and its parts (`CandidateProfile`, `ContactInformation`,
  `WorkExperienceEntry`, `EducationItem`, `Certification`, `SkillCategory`,
  `Project`, `LanguageItem`, `Award`, `Publication`, `ProfessionalLink`), the
  five per-call groups and `SECTION_GROUPS` / `ALL_SECTIONS`,
  `parse_year_month`, `total_experience_months`, `format_experience_length`,
  `normalize_years_of_experience`, `settle_years_of_experience`.

## Not in scope

The prompt and the model call (the host's, through `asas-llm`), storage of
the document or the photo (`asas-storage`), duplicate detection, OCR of a
scanned CV, and Markdown conversion that keeps layout (a host that wants
tables as Markdown rather than pipe-joined rows brings its own converter).

Extracted from a production hiring platform whose two tiers each read CVs,
with different stacks, and whose headshot extractor was rewritten once after
the first version found no face in any real CV.
