# Changelog: `asas-cv`

Versions follow semver, and the git tag matches this file: `asas-cv/v0.1.0`.
Pre-1.0, a breaking change bumps the **minor**.

Release procedure and the historical tag mapping: [`RELEASING.md`](../../RELEASING.md).

## 0.1.0 (unreleased)

First release. Extracted from a production hiring platform: the text
extractor from its application tier, the headshot extractor and the parsed
CV's shape from its AI tier, each made independent of the product.

- **`text`**: `extract_text` for PDF (pdfminer), Word `.docx` (python-docx,
  the body walked in document order so tables stay under their headings)
  and plain text or Markdown (UTF-16 on a BOM only, cp1256 before latin-1);
  refusals as `ExtractionError` with one of five codes; `cut_on_a_boundary`
  and `normalize`.
- **`photo`**: `extract_profile_photo`, which never raises: image XObjects
  page by page (forms included) through pdfminer's own stream decoding, so
  filter chains and predictors are handled; a JPEG or PNG passed through,
  raw samples re-encoded as a JPEG, CMYK skipped; `word/media` for a .docx;
  the size rule as `looks_like_a_headshot`.
- **`resume`**: `ParsedCVSections` and its parts as Pydantic models, the
  five non-overlapping per-call groups (`SECTION_GROUPS`), durations
  computed from dates (`WorkExperienceEntry.duration_months`,
  `parse_year_month`), `total_experience_months`,
  `format_experience_length`, `normalize_years_of_experience` and
  `settle_years_of_experience`.
