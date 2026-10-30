"""Section headings found in code, by the kind of section they open.

A heading tells the rest of the package where a list sits: which lines a list section
asked again or given a call of its own is sent (:mod:`.followups`), what a skills list
is (:mod:`.skills`), how many items a numbered list should give, and the topic of the
lines no field took (:mod:`.other`). A line is a heading when it is short, has no digits,
and is set out as one or is nothing but section names.

The English words follow Resume-Matcher's heading list (ats_parse/headings.py,
Apache-2.0); it has no Arabic, so the Arabic words are ours. Words are matched after
:func:`asas_cv_profile.checks.norm`, so hamza, alef and teh marbuta forms do not
matter. A host adds words for one section with ``headings:`` in its profile model.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from .arabic import ARABIC_RE
from .checks import norm

#: kind of section -> words that, in a short heading line, name it
WORDS: dict[str, tuple[str, ...]] = {
    "work": ("experience", "work experience", "professional experience", "employment history", "employment", "work history", "career history", "positions", "appointments",
             "الخبرات", "الخبرة", "الخبرات العملية", "الخبرة العملية", "الخبرة المهنية", "السجل الوظيفي"),
    "education": ("education", "academic background", "academic details", "academic qualifications", "qualifications",
                  "التعليم", "المؤهلات", "المؤهل العلمي", "المؤهلات العلمية", "التحصيل العلمي"),
    "certifications": ("certifications", "certificates", "certification", "licenses", "licences", "الشهادات", "الشهادات المهنية"),
    "courses": ("courses", "training", "workshops", "professional development", "الدورات", "الدورات التدريبية", "التدريب"),
    "publications": ("publications", "publication", "papers", "journal articles", "journals", "refereed journals", "referred journals",
                     "research papers", "published papers", "articles", "scholarly", "conference proceedings",
                     "المنشورات", "الأبحاث", "البحوث", "الأبحاث المنشورة", "الأوراق العلمية"),
    "talks": ("talks", "presentations", "conferences", "المؤتمرات"),
    "patents": ("patents", "patent", "براءات الاختراع"),
    "awards": ("awards", "honors", "honours", "recognitions", "الجوائز", "الجوائز والتكريمات"),
    "memberships": ("memberships", "affiliations", "professional bodies", "العضويات"),
    "projects": ("projects", "المشاريع"),
    "volunteering": ("volunteering", "volunteer", "community service", "التطوع", "العمل التطوعي"),
    "languages": ("languages", "اللغات"),
    "skills": ("skills", "technical skills", "core skills", "key skills", "core competencies", "competencies", "المهارات", "الكفاءات"),
}
#: headings that end a section without naming one of the kinds above
OTHER = ("summary", "profile", "objective", "references", "reference", "personal details", "personal information",
         "contact", "interests", "hobbies", "additional information", "other information", "achievements", "key achievements",
         "accomplishments", "declaration",
         "الملخص", "نبذة", "المعلومات الشخصية", "البيانات الشخصية", "المراجع", "الهوايات", "معلومات إضافية", "الإنجازات")
#: the kinds nearly every CV has a heading for; a long file with fewer than two of them is some other document
CORE = ("work", "education", "skills", "languages", "certifications")

_MARKS = re.compile(r"^[\s#*_>|-]*(?:\d+[.)]\s*)?|[\s*_:|-]*$")
_JOIN = re.compile(r"\s+(?:and|&|/|,)\s+|\s*[&/,]\s*|\s+و", re.IGNORECASE)


class Headings:
    """The heading words in force: the defaults above, plus a host's own per kind."""

    def __init__(self, extra: Mapping[str, Sequence[str]] | None = None):
        words = {kind: list(ws) for kind, ws in WORDS.items()}
        for kind, ws in (extra or {}).items():
            words.setdefault(kind, []).extend(ws)
        self.words = {kind: tuple(norm(w) for w in ws) for kind, ws in words.items()}
        self.other = tuple(norm(w) for w in OTHER)
        # one pattern per kind (a word as a whole run of words in the line) and one set of
        # every word, built once: a line is matched in one pass per kind, not once per word
        self._kinds = {kind: _any_of(ws) for kind, ws in self.words.items() if ws}
        self._other = _any_of(self.other)
        self._names = frozenset(w for ws in (*self.words.values(), self.other) for w in ws)

    def heading(self, line: str) -> set[str] | None:
        """The kinds a line heads, an empty set for a heading of another kind, None for
        a line that is not a heading. A heading has at most six words and no digits,
        and is set out as one or is nothing but section names ("Languages", "Honours
        and Awards"), so "Project Manager" in plain type is not one."""
        bare = _MARKS.sub("", line)
        if not bare or any(ch.isdigit() for ch in bare) or len(bare.split()) > 6 or bare.endswith((".", ";", ",")):
            return None
        parts = [norm(p) for p in _JOIN.split(bare) if p.strip()]
        only_names = all(p in self._names for p in parts)
        if not (only_names or _formatted(line, bare)):
            return None
        n = norm(bare)
        kinds = {kind for kind, pattern in self._kinds.items() if pattern.search(n)}
        if any(has(n, w) for w in ("editor", "editorial", "reviewer", "reviewing")):
            kinds.discard("publications")  # "Editor and reviewer of journals" lists journals, not papers
            if not kinds:
                return set()
        if kinds and any(has(n, w) for w in ("taught", "teaching", "تدريس")):
            kinds.discard("courses")  # "Courses taught" lists teaching, not courses attended
            if not kinds:
                return set()
        if kinds:
            return kinds
        return set() if self._other.search(n) else None

    def find(self, lines: list[str], other_ends: bool = True) -> dict[str, list[int]]:
        """Each kind with a heading in the text, and the line numbers under its headings:
        from the heading to the next heading of any kind, or with ``other_ends=False`` to
        the next heading of a known kind, so a stray "Profile" or "References" (a Google
        Scholar printout's own header, say) does not cut a list short."""
        marks = [(n, h) for n, line in enumerate(lines, 1) if (h := self.heading(line)) is not None and (h or other_ends)]
        out: dict[str, list[int]] = {}
        for i, (n, kinds) in enumerate(marks):
            end = marks[i + 1][0] - 1 if i + 1 < len(marks) else len(lines)
            for kind in (k for k in self._kinds if k in kinds):  # the table's order, never a set's, so a run is repeatable
                out.setdefault(kind, []).extend(range(n, end + 1))
        return out

    def looks_like_cv(self, lines: list[str]) -> bool:
        return sum(1 for kind in self.find(lines) if kind in CORE) >= 2


def has(line: str, word: str) -> bool:
    """``word`` (normalised) stands in ``line`` as whole words."""
    return re.search(rf"(?:^| ){re.escape(word)}(?: |$)", line) is not None


def _any_of(words: tuple[str, ...]) -> re.Pattern:
    """Any of ``words`` as whole words, longest first so a phrase wins over its first word."""
    alternatives = "|".join(re.escape(w) for w in sorted(set(words), key=len, reverse=True))
    return re.compile(rf"(?:^| )(?:{alternatives})(?: |$)")


def _formatted(line: str, bare: str) -> bool:
    """Set out as a heading: a Markdown heading, capitals, a closing colon, or Arabic
    (which has no capitals) of four words or fewer."""
    if line.lstrip().startswith("#") or line.rstrip().endswith(":"):
        return True
    if ARABIC_RE.search(bare):
        return len(bare.split()) <= 4
    letters = [ch for ch in bare if ch.isalpha()]
    return bool(letters) and all(ch.isupper() for ch in letters)


def marked(line: str) -> bool:
    """A Markdown heading of six words or fewer and no digits, as Document Intelligence
    marks the headings it sees, whatever words they use."""
    bare = _MARKS.sub("", line)
    return line.lstrip().startswith("#") and bool(bare) and len(bare.split()) <= 6 and not any(ch.isdigit() for ch in bare)


def title(line: str) -> str:
    """A heading line's own words, without its marks."""
    return _MARKS.sub("", line).strip()


#: a line that opens a list item: "[12]", "12.", "12)", or a bullet
ITEM = re.compile(r"^\s*(?:#+\s*)?(?:\[\d{1,4}\]|\d{1,3}[.)]\s|[-•*▪●◦‣]\s)")
NUMBERED = re.compile(r"^\s*(?:#+\s*)?(?:\[\d{1,4}\]|\d{1,3}[.)]\s)")
#: lines per call for a long section: the model keeps about a hundred entries in one answer, not more
CHUNK = 100


def chunks(nums: list[int], lines: list[str], size: int = CHUNK) -> list[list[int]]:
    """A long section's lines cut into runs of about ``size`` lines, each cut where an
    item starts, so no entry is split; a run with no item start is cut at 1.5 x size."""
    out: list[list[int]] = [[]]
    for n in nums:
        current = out[-1]
        opens = bool(ITEM.match(lines[n - 1]))
        if current and ((len(current) >= size and opens) or len(current) >= size * 3 // 2):
            out.append([])
        out[-1].append(n)
    return [c for c in out if c]


def numbered_items(nums: list[int], lines: list[str]) -> int:
    """How many lines in the section open a numbered item: what the profile should hold."""
    return sum(1 for n in nums if NUMBERED.match(lines[n - 1]))
