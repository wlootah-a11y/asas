# Changelog: `asas-cv-profile`

Versions follow semver, and the git tag matches this file: `asas-cv-profile/v0.1.0`.
Pre-1.0, a breaking change bumps the **minor**.

Release procedure and the historical tag mapping: [`RELEASING.md`](../../RELEASING.md).

## 0.1.0 (unreleased)

First release.

- **Profile model**: the host's sections and fields in YAML, with section kinds,
  field roles, computed fields and the region phone numbers are read in; checked on load.
- **Lines**: `read_cv` with Document Intelligence for PDFs (`LayoutReader`, the
  `docintel` extra: the first 40 pages read, the file and its result deleted from
  Azure after each read, a failed one too; a key or an Entra ID credential; a read
  that takes over `read_timeout` seconds refused as `reader_unavailable`) and asas-cv
  for Word and text; refusals as `Refused` with a code. A page header or footer that
  is not repeated across pages is kept as a line, and a read text that lacks a year
  or 1% of the words in the PDF's own text layer gets a warning.
- **Calls**: `Asker` runs the package's profile-builder release on asas-agent, with a
  cap on calls at once and tokens-a-minute and requests-a-minute buckets; each request
  is counted whole (instructions, output schema and message). A failed call is recorded by its error's
  type and HTTP status, never its message (which can repeat CV text); `raises` names the
  errors a host wants raised instead, such as its guard's refusal.
- **Build**: `ProfileBuilder.build`, `recheck` (no model call) and `other_again`;
  values checked against their lines, follow-up calls only where the text shows a
  need, twins folded (and flagged where dates alone chose the pair), skills kept in
  the CV's words, durations and totals with overlaps counted once, the lines no field
  took kept as other information. Dates are read in every Arabic region's month names,
  as Hijri dates by month name or in numbers in any order, and both ways where day and
  month, or day and two-digit year, could swap; a Hijri year copied as the date becomes
  its Gregorian date. Arabic letters are folded with CAMeL Tools' character maps, held
  in the package (camel-tools itself brings torch and transformers, which the reader never uses). A build makes two calls at most by default (`followups="one"`):
  one reading and one follow-up carrying every gap; `followups="each"` gives each gap a call. `keep_all` keeps every line no field took in
  other information; `keep_personal` keeps personal facts in a block of their own
  marked restricted, never sent to a model call by the package (neither to the naming
  call nor as lines left to place in the follow-up). A personal line goes to that block even when a field
  cites it, and the facts are found as CVs write them (DOB, birth date, "Age 33",
  "Children: 2", and their Arabic forms). `personal(line)` is exported, so a host that stores
  a reading can screen it with the same rule.
- **Command line** `asas-cv-profile` (`schema`, `one`, `run`, `recheck`, `other`,
  `serve`; `run --cvs-at-once` reads CVs together under the same limits, and a release
  can come from a release store with `--store` and `--label`, or from an agents folder at
  another reasoning effort with `--effort`) and the upload page (`upload_router`, the `serve` extra).
- **Files and the page, by OWASP's File Upload and CSRF cheat sheets**: a file whose
  first bytes are not the type its name claims is refused unread, and so is a Word
  file that unpacks past 100 MB or 1,000 parts; the page refuses an upload from
  another site's page, `serve` answers only to this machine's Host names, and the
  upload is kept under the type it was read as.
