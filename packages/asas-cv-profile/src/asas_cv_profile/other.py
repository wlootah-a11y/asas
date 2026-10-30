"""The lines no field took: shown as runs worth a look, and kept as other information.

Every content line no field took goes to the section of kind ``other_information``, one
entry per heading it sits under (the field with role ``topic``: the heading; ``details``:
the lines), so nothing the CV says is lost. Left out: lines under a referees or
personal-details heading, personal facts, and lines with an email, a link or a phone
number. With ``keep_all``, the part of a line a field took only part of is kept too: the
characters no value's span covers (:mod:`.spans`), a sentence of them at a time, unless it
shares a word or number with a value in the model's own words citing the line (a date, a
choice). Each block's details carry the spans of exactly what it keeps. One short call names each block's kind and the lines elsewhere that already say
what it says; code checks that claim before a block is left out as a repeat.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import calls, checks, spans
from .answers import cited_text, personal
from .headings import Headings, has, marked, title
from .lines import Lines
from .model import ProfileModel, Section, Spec
from .seams import Ask, CallResult

#: content lines in a row that no entry cites, worth a look
UNCITED_RUN = 8
#: headings whose lines the profile keeps nowhere: referees are other people, and
#: personal details are either fields already or facts the profile must not hold
KEEP_OUT = ("references", "reference", "referees", "personal details", "personal information", "contact",
            "المراجع", "المعلومات الشخصية", "البيانات الشخصية")
CONTACT = re.compile(r"@|https?://|www\.|(?<!\d)(?:\+|00)?\d(?:[\s().-]?\d){8,}(?!\d)", re.IGNORECASE)
#: a block is a repeat only when this share of its numbers and Latin words are in the
#: lines it repeats or in a line a field cites: the model says which lines, code checks it
REPEAT_MARKS = 0.8
_MARK = re.compile(r"[a-z][a-z+#.]+|\d+")
#: keep_all: a sentence of uncovered characters with at least this many marks, and none in
#: common with a value in the model's own words citing its line, is the rest of a line a
#: field took part of ("Holds a driving licence. Open to relocating."), kept as other
#: information so it is not lost
LEFT_OVER_MARKS = 3
_SENTENCE = re.compile(r"[^.!?;]+[.!?;]?")


def marks(text: str) -> set[str]:
    """Numbers and Latin words, a plural as its singular ("KPIs" is "KPI")."""
    return {m[:-1] if len(m) > 3 and m.endswith("s") and not m.isdigit() else m for m in _MARK.findall(checks.norm(text))}


@dataclass(frozen=True)
class Target:
    """Where other information goes: the section, and its fields for the heading, the
    lines and (when it has one) the block's kind."""

    section: Section
    topic: Spec
    details: Spec
    kind: Spec | None


#: the topic of the block of personal facts a keep_personal build keeps apart
RESTRICTED_TOPIC = "Personal details (restricted)"
#: a block's kind from its heading, for a build that makes no call to name it: the first
#: of the host's kinds whose words the heading holds, else Other
HEADING_KINDS = {"Achievements": ("achievement", "award", "honour", "honor"), "References": ("reference", "referee"),
                 "Availability": ("availability", "notice"), "Personal details": ("personal",),
                 "Interests": ("interest", "hobbies", "hobby"), "Declaration": ("declaration",),
                 "Summary": ("summary", "profile", "objective", "about"), "Research and grants": ("research", "grant"),
                 "Teaching": ("teaching", "taught"), "Training": ("training", "workshop"), "Skills": ("skill", "competenc")}


class OtherLines:
    """The lines no field took, for one profile model. ``target`` is None when the model
    has no section of kind ``other_information`` with a topic and a details field: the
    lines are then still counted, and kept nowhere."""

    def __init__(self, model: ProfileModel, headings: Headings, keep_all: bool = False, keep_personal: bool = False):
        self.headings = headings
        # keep_personal: personal facts (marital status, religion, birth date and the like) are kept too,
        # in a block of their own marked restricted, for the host to keep out of every model input
        self.keep_personal = keep_personal
        # keep_all: lines under a personal-details, contact or references heading and lines with an email,
        # link or phone are kept too when no field took them; only personal facts stay out
        self.keep_all = keep_all
        self.section: Section | None = model.first("other_information")
        s = self.section
        topic, details = (s.role("topic"), s.role("details")) if s else (None, None)
        self.target = Target(s, topic, details, s.role("kind")) if s and topic and details else None

    @property
    def usable(self) -> bool:
        return self.target is not None

    def cited(self, profile: dict) -> set[int]:
        """Every line an entry or a text field cites, other information left out: it is made from the rest."""
        cited: set[int] = set()
        s = self.section
        for cat, subs in profile.items():
            for sub, node in subs.items():
                if s and (cat, sub) == (s.group, s.name):
                    continue
                for e in node if isinstance(node, list) else [node]:
                    cited.update(e.get("lines") or [])
                    for cell in (e.get("fields") or {}).values():
                        cited.update(cell.get("lines") or [])
        return cited

    def left_over(self, profile: dict, text: Lines) -> dict[int, list[tuple[int, int]]]:
        """For each cited line a field took only part of, the character ranges of what is
        left: sentences of characters no value's span covers, of LEFT_OVER_MARKS or more
        marks, sharing none with a value in the model's own words that cites the line."""
        s = self.section
        skip = (s.group, s.name) if s else None
        cover = spans.covered(profile, skip)
        said: dict[int, set[str]] = {}
        for cat, subs in profile.items():
            for sub, node in subs.items():
                if (cat, sub) == skip:
                    continue
                for e in node if isinstance(node, list) else [node]:
                    own: set[str] = set()
                    nums = set(e.get("lines") or [])
                    for cell in (e.get("fields") or {}).values():
                        nums.update(cell.get("lines") or [])
                        if cell.get("spans"):
                            continue
                        v = cell.get("value")
                        for x in v if isinstance(v, list) else [v]:
                            if isinstance(x, (str, int, float)) and not isinstance(x, bool):
                                own |= marks(str(x))
                    for n in nums:
                        said.setdefault(n, set()).update(own)
        out: dict[int, list[tuple[int, int]]] = {}
        for n in sorted(set(cover) | set(said)):
            line = text.line(n) or ""
            for a, b in spans.gaps(len(line), cover.get(n, [])):
                for m in _SENTENCE.finditer(line, a, b):
                    part = m.group()
                    found = marks(part)
                    if len(found) >= LEFT_OVER_MARKS and not found & said.get(n, set()):
                        start = m.start() + len(part) - len(part.lstrip())
                        out.setdefault(n, []).append((start, m.end() - (len(part) - len(part.rstrip()))))
        return out

    def uncited(self, profile: dict, text: Lines) -> list[list[int]]:
        """Runs of UNCITED_RUN or more content lines no entry and no text field cites, as
        [first, last, count]: something missed, or what the profile has no field for.
        Other information does not count as citing them, so a miss still shows. A line
        stating a personal fact is never a miss (no field takes one), so it is never
        sent to be placed."""
        cited = self.cited(profile)
        runs: list[list[int]] = []
        current: list[int] = []
        for n, line in enumerate(text.lines, 1):
            if n in cited or personal(line):
                if current:
                    runs.append(current)
                current = []
            elif sum(ch.isalpha() for ch in line) >= 3 and self.headings.heading(line) is None:
                current.append(n)
        if current:
            runs.append(current)
        return [[r[0], r[-1], len(r)] for r in runs if len(r) >= UNCITED_RUN]

    def blocks(self, profile: dict, text: Lines, skip: frozenset[int] = frozenset()) -> list[dict]:
        """Every content line no field took, one entry per heading it sits under. ``skip``:
        lines found to repeat what the profile already holds."""
        if (t := self.target) is None:
            return []
        cited = self.cited(profile) | skip
        left = self.left_over(profile, text) if self.keep_all else {}
        cited -= set(left) - skip
        keep_out_words = tuple(checks.norm(w) for w in KEEP_OUT)
        found: dict[int, tuple[str, list[int]]] = {}
        restricted: list[int] = []
        at, topic, keep_out = 0, "Before any heading", False
        for n, line in enumerate(text.lines, 1):
            h = self.headings.heading(line)
            if h is None and n not in cited and marked(line):
                h = set()  # "##### ADDITIONAL INFORMATION": a heading the word lists do not know is still a new topic
            if h is not None:
                at, topic = n, title(line)
                keep_out = not h and any(has(checks.norm(topic), w) for w in keep_out_words)
                continue
            if personal(line):  # before the cited test: a field may cite a line that also states a personal fact
                if self.keep_personal:
                    restricted.append(n)
                continue
            if n in cited or sum(ch.isalpha() for ch in line) < 3:
                continue
            if not self.keep_all and (keep_out or CONTACT.search(line)):
                continue
            found.setdefault(at, (topic, []))[1].append(n)
        def kept(nums: list[int]) -> list[list[int]]:
            """The spans of what a block keeps: the left-over part of a line a field took, else the whole line."""
            return [[n, a, b] for n in nums for a, b in (left.get(n) or [(0, len(text.line(n) or ""))])]

        out = [{"lines": nums, "fields": {
                    t.topic.name: {"kind": "copied", "check": "unplaced", "value": topic},
                    t.details.name: {"kind": "text", "lines": nums, "check": "unplaced", "spans": kept(nums),
                                     "value": "\n".join(text.line(n) or "" for n in nums)}}}
               for _, (topic, nums) in sorted(found.items())]
        if restricted:
            out.append({"lines": restricted, "restricted": True, "fields": {
                t.topic.name: {"kind": "copied", "check": "unplaced", "value": RESTRICTED_TOPIC},
                t.details.name: {"kind": "text", "lines": restricted, "check": "unplaced", "spans": kept(restricted),
                                 "value": "\n".join(text.line(n) or "" for n in restricted)}}})
        return out

    def to_name(self, profile: dict) -> bool:
        """Whether a block is left for the naming call: personal facts kept apart are not."""
        return any(not e.get("restricted") for e in self.held(profile))

    def held(self, profile: dict) -> list[dict]:
        s = self.section
        return (profile.get(s.group) or {}).get(s.name) or [] if s else []

    def counts(self, profile: dict) -> dict:
        other = self.held(profile)
        return {"lines": sum(len(e["lines"]) for e in other), "topics": len(other)}

    def kinds_by_heading(self, profile: dict) -> dict:
        """Each block's kind from its heading, with no call: no block is found to repeat
        others, so a bilingual CV's second copy stays. How many lines and topics it holds."""
        self._kind_by_heading(self.held(profile))
        return self.counts(profile)

    def _kind_by_heading(self, blocks: list[dict]) -> None:
        t = self.target
        if t is None or t.kind is None:
            return
        values = set(t.kind.values or calls.OTHER_KINDS)
        for e in blocks:
            n = checks.norm((e["fields"].get(t.topic.name) or {}).get("value") or "")
            kind = next((k for k, words in HEADING_KINDS.items() if k in values and any(w in n for w in words)),
                        "Other" if "Other" in values else None)
            if kind:
                e["fields"][t.kind.name] = {"kind": "normalised", "check": "unplaced", "value": kind}

    def put(self, profile: dict, text: Lines, skip: frozenset[int] = frozenset()) -> dict:
        """Other information made again from the profile as it stands, in place; how many
        lines and topics it holds. A block keeps the kind it was given before, matched by
        its first line."""
        if (t := self.target) is None:
            return {"lines": 0, "topics": 0}
        s, kind_f = t.section, t.kind
        before = profile.get(s.group, {}).pop(s.name, None) or []
        kinds = {e["lines"][0]: e["fields"][kind_f.name] for e in before if kind_f and kind_f.name in e["fields"] and e.get("lines")}
        other = self.blocks(profile, text, skip)
        if kind_f:
            for e in other:
                if e["lines"][0] in kinds:
                    e["fields"][kind_f.name] = kinds[e["lines"][0]]
        self._kind_by_heading([e for e in other if e.get("restricted")])
        if other:
            profile.setdefault(s.group, {})[s.name] = other
        return self.counts(profile)

    async def name(self, profile: dict, text: Lines, ask: Ask) -> tuple[CallResult, list[dict]]:
        """Each block gets its kind, and a block that only repeats lines the profile already
        holds is left out: a bilingual CV puts its Arabic summary, skills and header there,
        because the fields cite the English copy. A repeat must point at lines that stay out
        of other information (a field cites them, or they were left out on purpose, such as
        contact lines) or sit in an earlier block that stays; and the block's numbers and
        Latin words ("IPSAS", "RPA", "25") must be in those lines or in a line a field cites
        (a stray English degree line in an Arabic block). What was left out is listed."""
        if (t := self.target) is None:
            raise ValueError("the profile model has no other information section to name")
        s, topic_f, kind_f = t.section, t.topic, t.kind
        # personal facts kept apart (named by their heading in put) are not offered as a repeat to drop
        held_all = self.held(profile)
        restricted = [e for e in held_all if e.get("restricted")]
        blocks = [e for e in held_all if not e.get("restricted")]
        kinds = tuple(kind_f.values) if kind_f and kind_f.values else calls.OTHER_KINDS
        res = await calls.kind_other(ask, [(e["fields"][topic_f.name]["value"], e["lines"]) for e in blocks], text.numbered(), kinds)
        cited = self.cited(profile)
        covered = set(range(1, len(text.lines) + 1)) - {n for e in held_all for n in e["lines"]}
        held = marks(" \n".join(text.line(n) or "" for n in sorted(cited)))
        gone, dropped = set(), []
        said = {b.block: b for b in getattr(res.output, "blocks", None) or [] if 1 <= b.block <= len(blocks)}
        for i, e in enumerate(blocks, 1):
            b = said.get(i)
            if b is None:
                covered |= set(e["lines"])
                continue
            if kind_f:
                e["fields"][kind_f.name] = {"kind": "normalised", "check": "unplaced", "value": b.kind}
            reps = sorted({n for n in b.repeats if text.line(n) is not None and n not in e["lines"]})
            own = marks(" \n".join(text.line(n) or "" for n in e["lines"]))
            theirs = marks(cited_text(reps, text)) | held
            if reps and set(reps) <= covered and (not own or len(own & theirs) >= REPEAT_MARKS * len(own)):
                gone.add(i)
                dropped.append({"lines": e["lines"], "repeats": reps, "topic": e["fields"][topic_f.name]["value"]})
            else:
                covered |= set(e["lines"])
        if gone:
            kept = [e for i, e in enumerate(blocks, 1) if i not in gone] + restricted
            if kept:
                profile[s.group][s.name] = kept
            else:
                del profile[s.group][s.name]
        return res, dropped
