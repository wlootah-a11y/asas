"""The fields worked out in code from the facts the model read: never asked of it.

For a role (a section of kind ``work``): its months (``duration_months``) and whether
its start gives the month or only the year (``date_precision``). Over all roles: the
months worked, overlapping or back-to-back roles counted once, so two posts held at
once are not two careers (``total_months``), and the months between them
(``gap_months``). For a skill (kind ``skills``): the months of the roles that name it
in their ``skills_used`` field, overlaps counted once, and the last month of them (``months_used``,
``last_used``).

asas-cv's ``total_experience_months`` sums each role's months, which counts an overlap
twice; the union here does not. Dates are the profile's own, YYYY-MM or YYYY: a year
alone counts from mid-year, so a role stated in years is neither lengthened nor cut
short by half a year on average.
"""

from __future__ import annotations

import re
from datetime import date

from .model import ProfileModel

NOW = ("present", "now", "current")


def skill_key(s: str) -> str:
    """A skill's name for matching: case and spacing only, so "C++", "C#" and ".NET" stay apart."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s+#.]", " ", (s or "").lower())).strip().rstrip(".")  # a full stop ending a list item, not ".NET"


def union(spans: list[tuple[int, int]]) -> tuple[int, int]:
    """Months covered and months between, overlapping or back-to-back spans counted once."""
    if not spans:
        return 0, 0
    spans = sorted(spans)
    total = gap = 0
    s, e = spans[0]
    for a, b in spans[1:]:
        if a > e + 1:
            total += e - s + 1
            gap += a - e - 1
            s, e = a, b
        else:
            e = max(e, b)
    return total + e - s + 1, gap


def year_month(value, today: date) -> tuple[int, int, str] | None:
    """(year, month, precision) of a profile date: "Month" when it gives the month,
    "Year only" (counted from June) when it does not; "present" is ``today``."""
    s = str(value or "").strip()
    m = re.match(r"^(\d{4})-(\d{1,2})", s)
    if m and 1 <= int(m.group(2)) <= 12:
        return int(m.group(1)), int(m.group(2)), "Month"
    m = re.match(r"^(\d{4})$", s)
    if m:
        return int(m.group(1)), 6, "Year only"
    if s.lower() in NOW:
        return today.year, today.month, "Month"
    return None


def value(entry: dict, label: str | None):
    cell = (entry.get("fields") or {}).get(label) if label else None
    return cell.get("value") if isinstance(cell, dict) else None


def entries(profile: dict, group: str, name: str) -> list[dict]:
    x = (profile.get(group) or {}).get(name)
    return x if isinstance(x, list) else ([x] if isinstance(x, dict) else [])


def put(entry: dict, label: str, value) -> None:
    """A computed value, marked as one."""
    if value is not None:
        entry.setdefault("fields", {})[label] = {"value": value, "check": "computed", "kind": "computed"}


def single(profile: dict, group: str, name: str) -> dict:
    """The one entry of a section that holds one, made when it is not there."""
    node = profile.setdefault(group, {})
    if not isinstance(node.get(name), dict):
        node[name] = {"lines": [], "fields": {}}
    return node[name]


Span = tuple[int, int, dict, str | None]  # first and last month (year * 12 + month), the role, its skills field


def _roles(profile: dict, model: ProfileModel, today: date) -> list[Span]:
    """Each role's months and date precision, filled in place; the spans of those with both ends."""
    spans: list[Span] = []
    for work in model.of_kind("work"):
        start, end, current, skills = (work.role(r) for r in ("start", "end", "current", "skills_used"))
        duration, precision = work.computed("duration_months"), work.computed("date_precision")
        if start is None:
            continue
        for r in entries(profile, work.group, work.name):
            a = year_month(value(r, start.name), today)
            holds = current is not None and value(r, current.name) is True
            b = year_month(value(r, end.name if end else None), today) or (year_month("present", today) if holds else None)
            if a and precision:
                put(r, precision.name, a[2])
            if a and b:
                n = (b[0] - a[0]) * 12 + (b[1] - a[1]) + 1
                if n > 0:
                    if duration:
                        put(r, duration.name, n)
                    spans.append((a[0] * 12 + a[1], b[0] * 12 + b[1], r, skills.name if skills else None))
    return spans


def _totals(profile: dict, model: ProfileModel, spans: list[Span]) -> None:
    if not spans:
        return
    total, gap = union([(a, b) for a, b, *_ in spans])
    for section, spec in model.where("total_months"):
        put(single(profile, section.group, section.name), spec.name, total)
    for section, spec in model.where("gap_months"):
        put(single(profile, section.group, section.name), spec.name, gap)


def _skills(profile: dict, model: ProfileModel, spans: list[Span]) -> None:
    """A skill's months are the union of the roles that name it, as the total is: two
    posts at once that both name it are not twice the months."""
    used: dict[str, list[tuple[int, int]]] = {}
    for a, b, r, skills_used in spans:
        for k in {skill_key(str(name)) for name in value(r, skills_used) or []} - {""}:
            used.setdefault(k, []).append((a, b))
    for section in model.of_kind("skills"):
        named, months, last_used = section.role("name"), section.computed("months_used"), section.computed("last_used")
        if named is None:
            continue
        for s in entries(profile, section.group, section.name):
            k = skill_key(str(value(s, named.name) or ""))
            if k in used:
                if months:
                    put(s, months.name, union(used[k])[0])
                if last_used:
                    last = max(b for _, b in used[k])
                    put(s, last_used.name, f"{(last - 1) // 12}-{(last - 1) % 12 + 1:02d}")


def add_experience(profile: dict, model: ProfileModel, today: date) -> dict:
    """The computed fields the profile model names, filled in place; returns the profile."""
    spans = _roles(profile, model, today)
    _totals(profile, model, spans)
    _skills(profile, model, spans)
    return profile
