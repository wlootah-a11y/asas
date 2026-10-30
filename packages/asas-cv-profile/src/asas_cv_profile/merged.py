"""Entries that may be several items read as one: the merge every per-value check passes.

An entry whose own lines state two or more separate date ranges (or, for a section of
single dates, two different years on separate lines) is flagged. Ranges are compared by
what they say, so a bilingual CV's "2024 - Present" and "٢٠٢٤ حتى الآن" are one; a range
inside another (a role within the employer's tenure) is not counted; and only lines no
other entry of the list cites are read, so an entry that over-cites its neighbours is not
taken for a merge.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from .arabic import ARABIC
from .checks import DIGITS, named_month
from .lines import Lines
from .model import ProfileModel

_Y = r"(?:19|20)\d{2}"
_WHEN = rf"(?:(?:[A-Za-z{ARABIC}]{{3,9}}\.?,?\s+|\d{{1,2}}[/.-])?{_Y})"
_NOW = r"present|current|now|date|today|ongoing|الآن|حاليا|حالياً"
DATE_RANGE = re.compile(rf"({_WHEN})\s*(?:-|–|—|\bto\b|\btill\b|\buntil\b|إلى|الى|حتى)\s*({_WHEN}|{_NOW})", re.IGNORECASE)
YEAR = re.compile(rf"(?<![\d:/.\-]){_Y}(?![\d])")


def _point(text: str) -> tuple[int, int]:
    """(year, month) of one end of a range; month 0 when not given; (9999, 0) for "present"."""
    y = re.search(_Y, text)
    if not y:
        return (9999, 0)
    m = re.match(r"\s*(\d{1,2})[/.-]", text)
    named = None if m else named_month(text.strip())  # a month's name, in any language dateparser reads
    month = int(m.group(1)) if m and 1 <= int(m.group(1)) <= 12 else (named[1] if named else 0)
    return (int(y.group(0)), month)


def date_ranges(line: str) -> set[tuple[tuple[int, int], tuple[int, int]]]:
    return {(_point(a), _point(b)) for a, b in DATE_RANGE.findall(line)}


def _inside(r, other) -> bool:
    return r != other and other[0] <= r[0] and r[1] <= other[1]


def flag_merged(profile: dict, model: ProfileModel, text: Lines) -> int:
    """Each entry that may be several gets a ``flags`` note, in place; returns how many."""
    dated = {s.name: s.trait("dates") for s in model.sections if s.trait("dates")}
    count = 0
    for subs in profile.values():
        for sub, node in subs.items():
            if sub not in dated or not isinstance(node, list):
                continue
            ranged = dated[sub] == "range"
            cited_by = Counter(n for e in node for n in set(e.get("lines") or []))
            for entry in node:
                others = [f for f in entry.pop("flags", []) if f.get("code") != "merged"]  # its own, set again below; others stay
                if others:
                    entry["flags"] = others
                found: dict[Any, int] = {}
                for n in entry.get("lines") or []:
                    if cited_by[n] > 1:
                        continue
                    line = (text.line(n) or "").translate(DIGITS)
                    for key in (date_ranges(line) if ranged else {int(y) for y in YEAR.findall(line)}):
                        found.setdefault(key, n)
                if ranged:
                    found = {r: n for r, n in found.items() if not any(_inside(r, o) for o in found)}
                lines_with = sorted(set(found.values()))
                if len(found) >= 2 and len(lines_with) >= 2:
                    what = "date ranges" if ranged else "years"
                    entry.setdefault("flags", []).append({"code": "merged", "message": f"Its lines state {len(found)} separate {what} "
                                                          f"(lines {', '.join(map(str, lines_with))}): it may be several entries read as one."})
                    count += 1
    return count
