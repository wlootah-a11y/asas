# asas-cv-profile

A CV read into your own profile model, where every value cites the lines it came
from and code checks it against them. You describe the profile in one YAML file in
your own words; the package reads the CV into numbered lines, has an asas-agent
release fill your sections, and settles the rest in code.

- **Lines first, no model.** Azure Document Intelligence reads a PDF (layout,
  reading order, two columns, scans, Arabic); asas-cv reads Word and plain text.
  The text becomes `L001 ...` lines, the addresses every value cites. A reader that
  waits on a service runs off the event loop.
- **The model reads, code checks.** One call fills the profile, each entry citing
  line ranges. Every value is then checked against its lines: names and titles by
  fuzzy match after Arabic folding, dates in however the CV writes them (every
  Arabic region's month names, Hijri dates by month name or in numbers in any order), numbers by what they say,
  phones as numbers. A match must also keep the value's numbers ("Level 4" is not
  "Level 5") and marks ("C++" is not "C#"), and one short or one Latin word is found
  as a word ("R" is not in "reports", "Java" is not "JavaScript"). A value the lines
  do not hold is kept and flagged, never dropped.
- **Asked again only where needed.** A list with a heading that came back empty,
  a level no role was given, runs of lines no entry took, and parts that cited
  lines the text lacks each get one short call with only their own lines.
- **Nothing the CV says is lost.** Lines no field took are kept as other
  information under the heading they sit under, each block named by kind; a block
  that only repeats what the profile holds (a bilingual CV's second copy) is left
  out, and the claim is checked in code first.

Table-less variant of the Asas host contract: no session, no `migrate`, no
`seed`, no `configure`; the package reads no settings, the command line does. The
upload page is an optional router. `import asas_cv_profile` loads neither the
Agents SDK nor the matching libraries.

## Five minutes

```
asas-cv-profile[docintel] @ git+https://github.com/wlootah-a11y/asas.git@asas-cv-profile/v0.1.0#subdirectory=packages/asas-cv-profile
```

`asas-agent` and `asas-cv` are required by name, as asas-audit requires
asas-tenancy: pin them beside it.

```python
from pathlib import Path
import asas_agent, asas_cv_profile

model = asas_cv_profile.load_model(Path("profile.yaml"))
asker = asas_cv_profile.Asker(
    asas_cv_profile.default_release("gpt-5.1"),          # the package's profile-builder agent
    provider=asas_agent.gateway_provider(api_key=key, base_url=url, timeout=240, max_retries=5),
    tokens_per_minute=100_000,                           # the key's limit; 0 for none
)
builder = asas_cv_profile.ProfileBuilder(model, asker, pdf=asas_cv_profile.LayoutReader(endpoint, di_key))
# or with no key, as Microsoft recommends for production: LayoutReader(endpoint, credential=azure.identity.ManagedIdentityCredential())
# (the identity needs the Cognitive Services User role on the resource, and the endpoint
#  must be the resource's own subdomain: Entra ID does not work on regional endpoints)

try:
    built = await builder.build(raw, "cv.pdf")
except asas_cv_profile.Refused as refused:
    refused.code  # document_too_little_text | document_not_a_cv | reader_unavailable | ...

built["profile"]["Work experience"]["Roles"][0]["fields"]["Job title"]
# {"value": "Senior Data Analyst", "kind": "copied", "check": "verified"}
```

A host that keeps its agents in a store reads the release with
`asas_agent.fetch_release` instead of `default_release`, and one that screens every
model call passes `run_model=asas_agent.GuardedModel(...)` instead of a provider.

## The profile model

```yaml
name: Candidate profile
sections:
  - name: Roles                    # your name for it; unique
    group: Work experience         # where the profile nests it (optional)
    kind: work                     # what the code knows it as (optional)
    list: true                     # one entry per item
    headings: [career]             # heading words beside the built-in ones (needs a kind)
    fields:
      - {name: Job title, type: string}
      - {name: Employer, type: string, role: employer}
      - {name: Start date, type: date, role: start}
      - {name: End date, type: date, role: end}
      - {name: Current, type: boolean, role: current}
      - {name: What they did, type: text}
      - {name: Seniority, type: enum, values: [Junior, Senior], infer: true}
      - {name: Months, type: number, computed: duration_months}
```

- **Region**: `region`, an ISO 3166 code beside `name` (optional), is where a phone
  number with no country code is from.
- **Types**: `string`, `url`, `phone`, `text` (cited as lines, copied from them by
  code), `date`, `year`, `number`, `boolean`, `enum` and `multi_enum` (`values`,
  or the CV's own words without them). `note` is shown to the model; `infer: true`
  lets a value be worked out rather than copied; `copied: false` marks a string
  chosen from a list you keep.
- **Kinds** (`asas_cv_profile.KINDS`): `work`, `work_level`, `education`,
  `scholarships`, `courses`, `certifications`, `licences`, `career_breaks`,
  `projects`, `publications`, `patents`, `talks`, `awards`, `memberships`,
  `volunteering`, `student_activities`, `skills`, `languages`,
  `other_information`. A kind brings its heading words, whether its entries carry
  date ranges, whether a long list of it gets calls of its own, whether a second
  look may put lines in it, and whether a bilingual CV's twins are folded.
- **Roles** (`ROLES`): a work section's `start`, `end`, `current`, `employer`,
  `skills_used`; education's `institution`, `level`, `graduation`; skills' `name`;
  other information's `topic`, `details`, `kind`.
- **Computed** (`COMPUTED`): `duration_months` and `date_precision` on a role,
  `total_months` and `gap_months` anywhere (overlapping roles counted once),
  `months_used` and `last_used` on a skill. `computed: host` is yours to fill in
  `finish(profile, model, as_of) -> warnings`.

`load_model` refuses an unknown type, kind, role or computed name, and a name used
twice, and says where.

## Command line

```bash
export OPENAI_API_KEY=... OPENAI_MODEL=gpt-5.1 OPENAI_BASE_URL=https://...   # as asas-agent's evals read a gateway
export AZURE_DI_ENDPOINT=... AZURE_DI_KEY=...                                 # Document Intelligence, for PDFs

asas-cv-profile schema  --profile-model profile.yaml                 # the calls it packs into
asas-cv-profile one     cv.pdf --profile-model profile.yaml --out runs/a
asas-cv-profile run     cvs/   --profile-model profile.yaml --out runs/a   # what is done is kept
asas-cv-profile recheck --profile-model profile.yaml --out runs/a          # current rules, no model call
asas-cv-profile other   runs/a/*/profile.json --profile-model profile.yaml # other information again
asas-cv-profile serve   --profile-model profile.yaml --out runs/a          # upload page, 127.0.0.1:8765
```

The upload page answers only to this machine's Host names and refuses an upload sent
from another site's page. A file is read only when its first bytes match its type, and
a Word file only when it unpacks to under 100 MB in under 1,000 parts.

Options: `--finish module:function`, `--agents DIR` (your own profile-builder
folder), `--chat-completions` (a gateway without the Responses API),
`--tokens-per-minute` and `--requests-per-minute` (the key's limits), `--parallel`
(calls at once), `--cvs-at-once` (`run`: CVs read together, sharing those limits),
`--effort none|minimal|low|medium|high|xhigh` (the reasoning effort in place of the
agents folder's, so one prompt serves a large model at `low` and a small one at `none`;
not with `--store`, whose releases are pinned), `--retries`, `--timeout`, `--split`
(calls under 90 properties each, Azure's strict-schema cap being 100), `--followups each` (a call per gap instead of two calls at
most, below),
`--store file:DIR|langfuse` with `--label` (the release from a release store, as
asas-agent hosts read theirs; it must be for `OPENAI_MODEL`), `--keep-all` and
`--keep-personal` (other information, below), `--as-of` (the day "present"
counts to), `--force`. In `run`, a CV that fails outside its model calls is noted in
the summary, the others go on, and the command exits 1; the next run reads it again. Each CV leaves `text.txt`,
`profile.json` (everything, the raw answers included) and `review.md` (what needs
a look first, then every field with its value, check and lines). The output folder
holds personal data and is made readable by its owner only.

## What it gets right, so you do not have to

- **Line ranges, not quotes.** The model cites `[first, last]` (SmartResume's
  index pointers): a ten-line description is two numbers, text fields are copied
  from the lines by code, and a citation can be checked.
- **A bad range does not sink an entry.** A range past the end is dropped and the
  rest checked; only the parts with no real line are asked again, in one call.
- **One call over a long list stops partway.** On a CV over 250 lines, long lists
  at the back get calls of their own, cut where items start.
- **A month the CV does not give** is cut to the year, and **a Hijri year copied as
  the date** ("1436" for 12-28-1436) becomes the Gregorian month the lines' Hijri date
  falls in, by hijridate, when only one reading fits (`was` keeps the model's).
- **Skills stay in the CV's words.** A reworded skill is set back to the list item
  it came from, and items the model left out of a skills list are added
  (Resume-Matcher's splitter, Apache-2.0); each skill records whether it came from
  a skills list or elsewhere.
- **A bilingual CV states everything twice.** Twins are found by record-linkage
  blocking: an Arabic and a Latin entry that keep the same years (and level), one
  to one in CV order, or two entries with the same names and dates. The Latin one
  is kept, with the Arabic names beside it. Where other items share a pair's dates,
  CV order chose the pair, and the entry is flagged for a person to check.
- **Two posts at once are not two careers.** Totals, and each skill's months, are the
  union of the roles' months; asas-cv's own total sums them.
- **A bilingual CV's second skills list is not read twice.** An Arabic list splits as
  an English one does (on ، and ؛), but a skills line in a script the model read none of
  the skills in is the other copy and is left to other information.
- **Two calls at most.** By default (`followups="one"`) the whole CV is read in one
  call, then every gap (sections asked again, a list read only in part, lines no entry
  took) goes in one follow-up that names where each section's lines are; other
  information takes its kind from its heading instead of a third call.
  `followups="each"` gives each gap its own call instead. On 28 test CVs, with the same
  prompt, the cap found 76% of the items against 81% for a call per gap, at 1.4 calls a
  CV on average against 3.4 (13 at most) and 29% less cost.
- **Personal facts stay out.** Lines stating marital status, birth date, religion
  and the like are left out of every copied text and of other information. With
  `keep_personal=True` they are kept in one block of their own marked
  `"restricted": true`, named by its heading and never sent to a naming call; keep it
  out of your own model inputs too. `keep_all=True` keeps every other line no field
  took (contact and reference lines among them), so nothing of the CV is lost.
- **A page's edges are read.** Document Intelligence puts a page's header and footer
  in comments; a text that repeats across pages is a running header and goes, any
  other (an employer and dates at the top of page 2) stays as a line.
- **A part the reader missed is flagged.** The PDF's own text layer (pdfminer) is
  compared with the read text: a missing year, or 1% of its words (3 at least),
  gives a warning. A read that has not finished in `read_timeout` seconds (300) is
  refused as `reader_unavailable`, for a later run to read again.
- **Rate and concurrency are two limits.** A semaphore caps calls at once; a leaky
  bucket (aiolimiter) keeps tokens a minute under the key's limit, each request
  counted whole (instructions, output schema and message, with tiktoken), and a
  second keeps requests a minute under it, a tenth of each kept back; the OpenAI client retries throttled and failed calls. Both limits
  belong to the event loop they run in, and a host's second loop gets its own.
  tiktoken fetches its vocabulary once: a host with no way out seeds
  `TIKTOKEN_CACHE_DIR` in its image, or tokens are estimated from the text's length.
- **The same input gives the same output.** Headings are taken in the table's order,
  never a set's, and a recheck of a build changes nothing.

A date written `7/6/2011` counts as both 7 June and 6 July: the order differs from
country to country, and the lines alone cannot say. For the same reason `Jan 19`
counts as both 19 January and January 2019, unless a year follows it, and `رمضان 1436`
with no day counts as both Gregorian months the Hijri month spans. A reference list's
`2025 Sep` still reads as one date when the line breaks after the year. A phone number with no country
code is read in the profile model's `region` (`region: AE`); with no region, only by
its digits.

## Surface

`load_model`, `parse_model`, `personal` (whether a line states a personal fact), `ProfileModel`, `Section`, `Spec`, `ModelError`,
`KINDS`, `ROLES`, `COMPUTED`, `TYPES`, `BUDGET`; `read_cv`, `Lines`,
`LayoutReader`, `Refused`, `CODES` (asas-cv's codes, then this package's);
`Asker` (`raises=` the host's errors to raise, such as a guard's refusal; any other
failure is recorded by type, never by its message, which can repeat CV text), `default_release`; `ProfileBuilder` (`build`, `recheck`, `other_again`;
`one_call=False` packs the sections into calls under `BUDGET`, `followups="each"`
gives each gap a call instead of the default two-call cap, `keep_all` and `keep_personal` widen other information); the seams `Ask`
(with its `release`), `CallResult`, `PdfReader`, `Finish`; the output shapes `Built`,
`Entry`, `Cell`; `write_outputs`, `review`, `summary`; `upload_router` (the `serve`
extra; a refusal is a 422 whose `detail` holds its `code` and `message`).

## Not in scope

Mapping values to the host's lists (skills to a taxonomy, a company to a sector),
scoring or ranking, storing the CV, and the headshot (asas-cv has it).

## Developing

`pip install -e ../asas-agent -e ../asas-cv -e '.[dev]'` (what CI runs; add
`docintel` and `serve` to use them), then `pytest -q`. No network, no model, no real
CV: made-up documents, a made-up profile model and a scripted model.
