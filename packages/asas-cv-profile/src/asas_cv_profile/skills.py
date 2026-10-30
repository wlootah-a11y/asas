"""Skill lists split in code, so a skill is kept as the CV writes it.

The splitter below is copied from Resume-Matcher
(github.com/srbhr/Resume-Matcher, commit f6296a5, apps/backend/app/services/parser.py,
`_skill_line_items` and `_is_prose`, with their constants).
Copyright Saurabh Rai and Resume-Matcher contributors; licensed under the Apache
License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0). Changes from the
original: the Arabic comma and semicolon (، ؛) added to its separators, so an Arabic
list splits as an English one does; and a separator inside brackets does not split, so
"data strategy (expert, 5/5)" stays one item. `listed` and `copy_and_restore` below are ours.
"""
from __future__ import annotations

import re

from .arabic import ARABIC_RE
from .checks import norm
from .headings import Headings
from .model import ProfileModel

# ---------------------------------------------------------------- from Resume-Matcher (Apache-2.0)

# A source line counts as a skill list when it has at least this many short items
# and at least _MIN_LINE_ANCHORS of them were parsed as skills.
_MIN_SKILL_LINE_ITEMS = 3
_MIN_LINE_ANCHORS = 2
_MAX_SKILL_ITEM_WORDS = 5
_MAX_SKILL_ITEM_CHARS = 40
# A real skill line has far fewer items; longer lines are rejected without being
# fully split, so a hostile line can't be expanded into huge lists.
_MAX_SKILL_LINE_ITEMS = 200
# No surrounding \s*: it backtracks quadratically on a long run of whitespace.
# Chunks are stripped instead.
_SKILL_SEPARATOR_RE = re.compile(r"[,|;•·،؛]")  # the Arabic comma and semicolon added here
_LIST_MARKER_RE = re.compile(r"^\s*(?:[-*+●▪◦‣]|\d+[.)])\s+")
_LINE_LABEL_RE = re.compile(r"^[^:,]{1,80}:\s*")
# PDF text extraction renders a visual gap (e.g. after a bold label) as 2+ spaces.
_EXTRACTION_GAP_RE = re.compile(r"\s{2,}")
_FINAL_CONJUNCTION_RE = re.compile(r"^(?:and|or|&)\s+", re.IGNORECASE)
# "Rust and Go" closing a list without an Oxford comma holds two skills; "&"
# stays whole: "Drag & Drop", "R & D".
_INLINE_CONJUNCTION_RE = re.compile(r"\s+(?:and|or)\s+", re.IGNORECASE)
_SKILL_HEADING_RE = re.compile(
    r"^(?:(?:technical|core|key|other)\s+)?"
    r"(?:skills?|technologies|tools|frameworks?|platforms?|tech(?:nology)?\s+stack|stack)$",
    re.IGNORECASE,
)
# Function words that mark a phrase as prose ("Built services with Python").
_PROSE_WORDS = frozenset(
    {"with", "for", "to", "the", "of", "in", "on", "using", "by", "from", "at", "into"}
)
# Function words that also appear inside names ("Ruby on Rails", "Internet of Things").
_NAME_CONNECTORS = frozenset({"on", "of", "the", "in", "for"})


def _skill_line_items(line: str, anchors: set[str]) -> list[str]:
    """Items of a skill-list line, or [] if the line isn't a list of short items.

    ``anchors`` are the casefolded skills the parser found; a gap-separated
    first segment is kept only when it is one of them (otherwise it is a label
    such as "Stack").
    """
    text = _LIST_MARKER_RE.sub("", line).strip()
    text = _LINE_LABEL_RE.sub("", text)  # "Tools: Python, Rust" -> "Python, Rust"

    chunks = _split_outside_brackets(text, _MAX_SKILL_LINE_ITEMS)
    if len(chunks) > _MAX_SKILL_LINE_ITEMS:
        return []
    items: list[str] = []
    for index, chunk in enumerate(chunks):
        gap_pieces = _EXTRACTION_GAP_RE.split(
            chunk.strip(), maxsplit=_MAX_SKILL_LINE_ITEMS
        )
        segments = [seg for seg in gap_pieces if seg]
        if index == 0 and len(segments) > 1 and segments[0].casefold() not in anchors:
            segments = segments[1:]  # "Stack  TypeScript": drop the label
        items.extend(segments)
        if len(items) > _MAX_SKILL_LINE_ITEMS:
            return []
    # Keep a leading dot (".NET"); drop trailing sentence punctuation.
    items = [item.strip().rstrip(".").strip() for item in items]
    items = [item for item in items if item]
    if items and _SKILL_HEADING_RE.match(items[0]):
        items = items[1:]  # "Technical Skills | Python | React"
    # A conjunction opening an item mid-line, or left dangling at the end, means a
    # sentence wrapped across lines ("..., and Redis, ..., and search and").
    if any(_FINAL_CONJUNCTION_RE.match(item) for item in items[:-1]) or any(
        item.split()[-1].casefold() in {"and", "or", "&"} for item in items
    ):
        return []
    if items:
        # "Python, Rust, and Go" and "Python, Rust and Go". The split is bounded;
        # an over-long last item is rejected below anyway.
        last = _FINAL_CONJUNCTION_RE.sub("", items.pop())
        items.extend(_INLINE_CONJUNCTION_RE.split(last, maxsplit=_MAX_SKILL_ITEM_WORDS))

    if len(items) < _MIN_SKILL_LINE_ITEMS:
        return []
    for item in items:
        words = item.split()
        # Prose and contact or location details are not skill lists.
        if (
            len(item) > _MAX_SKILL_ITEM_CHARS
            or len(words) > _MAX_SKILL_ITEM_WORDS
            or "@" in item
            or "://" in item
            or _is_prose(words)
        ):
            return []
    return items


_OPEN, _CLOSE = "([{", ")]}"


def _split_outside_brackets(text: str, maxsplit: int) -> list[str]:
    """``_SKILL_SEPARATOR_RE.split``, but a separator inside brackets does not split:
    "data strategy (expert, 5/5), SQL" is two items. Linear in the line's length."""
    chunks, start, depth = [], 0, 0
    for i, ch in enumerate(text):
        if ch in _OPEN:
            depth += 1
        elif ch in _CLOSE:
            depth = max(0, depth - 1)
        elif depth == 0 and _SKILL_SEPARATOR_RE.fullmatch(ch):
            chunks.append(text[start:i])
            start = i + 1
            if len(chunks) > maxsplit:
                break
    chunks.append(text[start:])
    return chunks


def _is_prose(words: list[str]) -> bool:
    """A phrase with a function word is prose unless it reads like a name.

    "Built services with Python" is prose; "Ruby on Rails" is a skill because
    every other word is capitalized (or starts with a digit or symbol).
    """
    function_words = {w.casefold() for w in words} & _PROSE_WORDS
    if not function_words:
        return False
    if function_words - _NAME_CONNECTORS:
        return True  # "with", "using", "by", ... join clauses, not names
    return not all(
        not word[0].isalpha() or word[0].isupper()
        for word in words
        if word.casefold() not in _PROSE_WORDS
    )



# ---------------------------------------------------------------- ours
COPY = 85  # RapidFuzz token-set ratio between the model's wording and a list item to take the item instead
#: a trailing bracket a list puts after a skill ("Python (5/5)", "SQL (advanced)"): not part of
#: the skill's name when it is matched against the skills the model found
_TRAILING_BRACKET = re.compile(r"\s*[(\[][^()\[\]]*[)\]]\s*$")


def _key(name: str) -> str:
    """A skill as it is matched: casefolded, a trailing bracket left off."""
    return _TRAILING_BRACKET.sub("", name).strip().casefold() or name.casefold()


def listed(lines: list[str], headings: Headings) -> dict[int, list[str]]:
    """The items of the CV's own skill lists: every line under a skills heading, split by
    Resume-Matcher's rule, or one short item a line when the list runs one to a line.
    Code, not the model, so the same CV gives the same list on every run (skills are what
    varies most between two runs of one text)."""
    out: dict[int, list[str]] = {}
    for n in headings.find(lines).get("skills", []):
        line = lines[n - 1]
        if headings.heading(line) is not None:
            continue
        items = _skill_line_items(line, set())
        if not items:
            one = _LINE_LABEL_RE.sub("", _LIST_MARKER_RE.sub("", line).strip()).strip().rstrip(".").strip()
            words = one.split()
            if (one and any(ch.isalpha() for ch in one) and len(one) <= _MAX_SKILL_ITEM_CHARS
                    and len(words) <= _MAX_SKILL_ITEM_WORDS and "@" not in one and "://" not in one and not _is_prose(words)):
                items = [one]
        if items:
            out[n] = items
    return out


def _script(text: str) -> str:
    return "ar" if len(ARABIC_RE.findall(text)) > sum(c.isascii() and c.isalpha() for c in text) else "latin"


def _agreed(anchors: list[dict], name: str) -> dict[str, dict]:
    """The fields, other than the name, that every anchor holds with one and the same value."""
    if not anchors:
        return {}
    first = {k: v for k, v in anchors[0]["fields"].items() if k != name and v.get("value") not in (None, "", [])}
    return {k: {"kind": v.get("kind", "normalised"), "value": v["value"]} for k, v in first.items()
            if all((a["fields"].get(k) or {}).get("value") == v["value"] for a in anchors[1:])}


def copy_and_restore(profile: dict, lines: list[str], model: ProfileModel, headings: Headings) -> dict[str, int]:
    """The model's skills set against the CV's own skill-list lines, in place, for every
    section of kind ``skills`` whose name field has the role ``name``.

    A skill the check could not find in its lines, but which is close to an item of a
    skill list on or beside those lines, takes the item's wording; the model's words stay
    under ``was``. Items of a skill list the model left out are added, by Resume-Matcher's
    rule: a line is a skill list when it holds three or more short items and two of them
    are skills the model found. They go to the section that holds most of the line's
    skills. Each skill records ``source``: ``skills list`` or ``elsewhere``."""
    from rapidfuzz import fuzz

    label = {s.name: named.name for s in model.of_kind("skills") if (named := s.role("name")) is not None}
    subs = [s for s in model.of_kind("skills") if s.name in label]
    entries = {s.name: (profile.get(s.group) or {}).get(s.name) or [] for s in subs}
    home: dict[str, str] = {}
    for sub, items in entries.items():
        for e in items:
            value = (e["fields"].get(label[sub]) or {}).get("value")
            if isinstance(value, str):
                home.setdefault(_key(value), sub)
    if not home:
        return {"copied": 0, "restored": 0}
    split = {n: _skill_line_items(line, set(home)) for n, line in enumerate(lines, 1)}

    copied = 0
    for sub, items in entries.items():
        for e in items:
            cell = e["fields"].get(label[sub])
            if not cell or cell.get("check") != "unverified" or not isinstance(cell.get("value"), str):
                continue
            near = sorted({m for n in e["lines"] for m in (n - 1, n, n + 1) if split.get(m)})
            options = [(fuzz.token_set_ratio(norm(cell["value"]), norm(item)), item) for m in near for item in split[m]]
            best = max(options, default=None)
            if best and best[0] >= COPY:
                cell.update(was=cell["value"], value=best[1], check="verified")
                home.setdefault(_key(best[1]), sub)
                copied += 1

    restored = 0
    own = listed(lines, headings)
    # trusted the way Resume-Matcher trusts a line: two of its items are skills the model found too,
    # so a CV a reader broke into word fragments does not turn its fragments into skills
    if sum(1 for items in own.values() for i in items if _key(i) in home) < _MIN_LINE_ANCHORS:
        own = {}
    for n, items in own.items():  # the CV's skills section: every item, whatever the model took
        split[n] = items
    names = [s.name for s in subs]
    # a bilingual CV lists its skills twice: a line in a script the model read none of its
    # skills in is the other copy, already held, unless its own items anchor it
    scripts = {_script(k) for k in home}
    by_key = {_key(v): e for sub, items in entries.items() for e in items
              if isinstance(v := (e["fields"].get(label[sub]) or {}).get("value"), str)}
    for n, items in split.items():
        anchored = [home[_key(i)] for i in items if home.get(_key(i))]
        if len(anchored) < _MIN_LINE_ANCHORS and (n not in own or _script(" ".join(items)) not in scripts):
            continue
        sub = max(names, key=anchored.count) if anchored else names[0]
        # a restored item takes the fields its line's own skills agree on (one list may hold
        # both kinds of skill, told apart by a field): what the section did for a model that
        # keeps each kind in a section of its own
        shared = _agreed([by_key[_key(i)] for i in items if _key(i) in by_key], label[sub])
        for item in items:
            if _key(item) in home:
                continue
            home[_key(item)] = sub
            fields = {label[sub]: {"kind": "copied", "check": "verified", "value": item, "restored": True}}
            fields.update({k: {**v, "check": "inferred", "restored": True} for k, v in shared.items()})
            entries[sub].append({"lines": [n], "fields": fields})
            restored += 1
    groups = {s.name: s.group for s in subs}
    for sub, items in entries.items():
        for e in items:  # where each skill stands in the CV: its skills list, or a role, project or summary
            e["source"] = "skills list" if set(e.get("lines") or []) & set(own) else "elsewhere"
        if items:
            profile.setdefault(groups[sub], {})[sub] = items
    return {"copied": copied, "restored": restored, "listed": sum(len(v) for v in own.values())}
