"""The model's answers into the profile: line ranges read, every value checked against
its lines, text fields copied from them, and the parts that cited lines the text lacks
listed for one short call to put right.

A value is checked against its entry's lines and the line either side, since a value
often wraps across a line break. A range past the end or back to front is dropped, not
held against the rest: one such range once turned 98 values found in their other lines
into "bad lines", all on one 1,029-line CV.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

from rapidfuzz import fuzz

from . import checks
from .lines import Lines
from .model import Group, Section
from .seams import CallResult

#: words that mean a line carries personal facts the profile must not hold
NEVER = re.compile(
    r"\b(marital status|civil status|married|divorced|widowed|spouse|number of children|dependants|dependents|pregnan\w*|"
    r"religion|date of birth|d\.o\.b|dob|birth ?date|born on|place of birth|birth ?place|age\s*:|gender\s*:|sex\s*:)",
    re.IGNORECASE,
)
#: a personal label written as one at the start of a line or after a separator ("Age 33",
#: "| Children: 2"), so "average age of the fleet" or "children's hospital" stays a line of the CV
NEVER_LABEL = re.compile(r"(?:^|[|;,•·])\s*(?:age|children|kids)\s*[:\-]?\s*\d", re.IGNORECASE)
NEVER_AR = re.compile("الحالة الاجتماعية|متزوج|أعزب|عدد الأطفال|الأطفال\\s*:|الأولاد\\s*:|الديانة|تاريخ الميلاد|تاريخ الولادة|مكان الميلاد|مكان الولادة|العمر\\s*:|الجنس\\s*:")
#: a re-asked part: "<call>.<section>[<entry>]" or ".<field>" after it
REF = re.compile(r"^(\w+)\.(\w+)\[(\d+)\](?:\.(\w+))?$")
#: RapidFuzz token-sort ratio at which a second look's entry is one the list already holds
SAME_ITEM = 92


def personal(line: str | None) -> bool:
    return line is not None and bool(NEVER.search(line) or NEVER_LABEL.search(line) or NEVER_AR.search(line))


def spans(ranges: Any, size: int) -> tuple[list[int], bool]:
    """Line numbers from [first, last] ranges, in the order given, and whether every
    range was a real one (inside the text, first not after last)."""
    out: list[int] = []
    seen: set[int] = set()
    ok = True
    for r in ranges or []:
        if not (isinstance(r, list) and 1 <= len(r) <= 2 and all(isinstance(x, int) for x in r)):
            ok = False
            continue
        first, last = r[0], r[-1]
        if first > last or first < 1 or last > size:
            ok = False
        for n in range(max(first, 1), min(last, size) + 1):
            if n not in seen:
                seen.add(n)
                out.append(n)
    return out, ok


def cited_text(entry_lines: list[int], text: Lines) -> str:
    """The entry's lines and the lines either side: a value often wraps across a line break.
    Lines that are not next to each other in the CV are set apart by an empty line."""
    window = sorted({m for n in entry_lines for m in (n - 1, n, n + 1)})
    out = []
    for i, n in enumerate(window):
        if i:
            out.append(" \n" if n == window[i - 1] + 1 else " \n\n")
        out.append(text.line(n) or "")
    return "".join(out)


def items(output: Any, sub: Section) -> list:
    """The entries an answer holds for one section, a single one as a list of one."""
    raw = getattr(output, sub.slug, None)
    return raw if isinstance(raw, list) else ([raw] if raw is not None else [])


def title(sub: Section) -> str:
    return f"{sub.group} > {sub.name}" if sub.group else sub.name


def cells(profile: dict) -> Iterator[tuple[str, str, int, str, dict]]:
    """Every field of every entry: (group, section, entry index, field, cell)."""
    for cat, subs in profile.items():
        for sub, node in subs.items():
            for i, entry in enumerate(node if isinstance(node, list) else [node]):
                for label, cell in (entry.get("fields") or {}).items():
                    yield cat, sub, i, label, cell


def bad_refs(results: list[CallResult], groups: list[Group], text: Lines) -> list[dict]:
    """The parts of the answers whose line ranges the text does not have, or that cite none."""
    by_name = {g.name: g for g in groups}
    size = len(text.lines)
    out = []
    for res in results:
        if res.output is None or res.group not in by_name:
            continue
        for sub in by_name[res.group].subs:
            for i, item in enumerate(items(res.output, sub)):
                filled = {spec.name: getattr(item, spec.slug, None) for spec in sub.asked}
                filled = {k: v for k, v in filled.items() if v not in (None, [], "")}
                if not filled:
                    continue
                what = f"{title(sub)} #{i + 1}"
                preview = json.dumps({k: v for k, v in filled.items() if not isinstance(v, list)}, ensure_ascii=False)[:160]
                nums, ok = spans(item.lines, size)
                if not ok or not nums:
                    out.append({"ref": f"{res.group}.{sub.slug}[{i}]", "what": what, "preview": preview, "cited": item.lines})
                for spec in sub.asked:
                    value = getattr(item, spec.slug, None)
                    if spec.kind == "text" and value and not spans(value, size)[1]:
                        out.append({"ref": f"{res.group}.{sub.slug}[{i}].{spec.slug}", "what": f"{what} > {spec.name}",
                                    "preview": preview, "cited": value})
    return out


def apply_fixes(results: list[CallResult], groups: list[Group], fixes: Any, asked: list[dict]) -> int:
    """The re-asked ranges put back into the answers; only refs that were asked."""
    by_name = {g.name: g for g in groups}
    outputs = {r.group: r.output for r in results if r.output is not None and r.group in by_name}
    wanted = {p["ref"] for p in asked}
    done = 0
    for fix in getattr(fixes, "fixes", None) or []:
        ref = fix.ref.strip()
        m = REF.match(ref)
        if ref not in wanted or not m or m.group(1) not in outputs:
            continue
        sub = next((s for s in by_name[m.group(1)].subs if s.slug == m.group(2)), None)
        found = items(outputs[m.group(1)], sub) if sub else []
        i = int(m.group(3))
        if i >= len(found):
            continue
        setattr(found[i], m.group(4) or "lines", fix.lines)
        done += 1
    return done


def identity(entry: dict, sub: Section) -> str:
    """What names an item: its copied values and its dates, as one comparable string."""
    f = entry["fields"]
    return checks.norm(" ".join(str(f[x.name]["value"]) for x in sub.asked
                                if x.name in f and f[x.name].get("value") and (x.kind == "copied" or x.type in ("date", "year"))))


def fold(have: list[dict], new: list[dict], sub: Section) -> list[dict]:
    """A second look's entries that are not already in the list. One that names an item
    the first answer holds (cited from a summary line, say, while its own lines went
    uncited) is folded in: its lines join that entry, so they are cited and not read as new."""
    kept = []
    keys = [identity(h, sub) for h in have]  # once each: folding changes an entry's lines, never its names
    for e in new:
        key = identity(e, sub)
        twin = next((h for h, k in zip(have, keys) if key and k and fuzz.token_sort_ratio(key, k) >= SAME_ITEM), None)
        if twin is None:
            kept.append(e)
        else:
            twin["lines"] = sorted(set(twin["lines"]) | set(e["lines"]))
    return kept


def entry_of(item: Any, sub: Section, text: Lines, notes: list[str]) -> dict | None:
    """One answered entry as the profile holds it: its lines, and each field with its
    value, how it relates to the CV and its check. None when every field is empty."""
    size = len(text.lines)
    nums, lines_ok = spans(item.lines, size)
    if not lines_ok and nums:
        notes.append(f"{title(sub)}: an entry cited a range the text does not have; its other lines were used.")
    entry_lines = sorted(nums)
    cited = cited_text(entry_lines, text)
    fields = {}
    for spec in sub.asked:
        value = getattr(item, spec.slug, None)
        cell: dict[str, Any] = {"kind": spec.kind}
        if spec.kind == "text":
            nums_t, _ = spans(value, size)
            kept = [n for n in nums_t if not personal(text.line(n))]
            if len(kept) < len(nums_t):
                notes.append(f"{title(sub)} > {spec.name}: left out {len(nums_t) - len(kept)} cited line(s) "
                             "with personal facts the profile must not hold.")
            cell["lines"] = kept
            value = "\n".join(text.line(n) or "" for n in kept) if kept else None
            cell["check"] = checks.check_value(spec, value, cited, bool(nums_t))
        else:
            cell["check"] = checks.check_value(spec, value, cited, bool(nums))
            if cell["check"] == "unverified" and (fixed := checks.corrected_date(spec, value, cited)):
                cell["was"], value, cell["check"] = value, fixed, checks.check_value(spec, fixed, cited, True)
        cell["value"] = value
        if cell["check"] != "empty":
            fields[spec.name] = cell
    return {"lines": entry_lines, "fields": fields} if fields else None


def assemble(results: list[CallResult], groups: list[Group], text: Lines, notes: list[str]) -> dict:
    """The profile the answers make: group > section > entries, a long section read in
    chunks as one list in text order, and a second look's entries folded into those the
    list already holds."""
    by_name = {g.name: g for g in groups}
    profile: dict[str, dict[str, Any]] = {}
    for res in results:
        if res.output is None or res.group not in by_name:
            continue
        for sub in by_name[res.group].subs:
            built = [e for item in items(res.output, sub) if (e := entry_of(item, sub, text, notes))]
            if not built:
                continue
            node = profile.setdefault(sub.group, {})
            if sub.repeating and sub.name in node:
                seen = {tuple(e["lines"]) for e in node[sub.name]}
                fresh = [e for e in built if tuple(e["lines"]) not in seen]
                if by_name[res.group].second_look:
                    fresh = fold(node[sub.name], fresh, sub)
                node[sub.name] += fresh
                node[sub.name].sort(key=lambda e: e["lines"][:1] or [0])
            elif not sub.repeating and sub.name in node and by_name[res.group].second_look:
                fill(node[sub.name], built[0])
            else:
                node[sub.name] = built if sub.repeating else built[0]
    return profile


def fill(held: dict, other: dict) -> None:
    """A follow-up's answer for a one-entry section the first answer already holds: it fills
    the fields the first left empty, and replaces a value its lines did not bear out with one
    they do; the rest stays."""
    took = False
    for label, cell in other["fields"].items():
        mine = held["fields"].get(label)
        if mine is None or (mine.get("check") == "unverified" and cell.get("check") == "verified"):
            held["fields"][label] = cell
            took = True
    if took:
        held["lines"] = sorted(set(held["lines"]) | set(other["lines"]))


def recheck_values(profile: dict, model_sections: list[Section], text: Lines) -> None:
    """The saved values checked again with the current rules, in place: no model call."""
    specs = {(s.group, s.name, spec.name): spec for s in model_sections for spec in s.asked}
    for cat, subs in profile.items():
        for sub, node in subs.items():
            for entry in node if isinstance(node, list) else [node]:
                lines = [n for n in entry.get("lines") or [] if text.line(n) is not None]
                for label, cell in entry.get("fields", {}).items():
                    spec = specs.get((cat, sub, label))
                    if spec is None or cell.get("check") == "computed":
                        continue
                    cell["kind"] = spec.kind
                    if spec.kind == "text":
                        own = [n for n in cell.get("lines") or [] if text.line(n) is not None]
                        cell["check"] = checks.check_value(spec, cell["value"], "", bool(own))
                    else:
                        cited = cited_text(lines, text)
                        cell["check"] = checks.check_value(spec, cell["value"], cited, bool(lines))
                        if cell["check"] == "unverified" and (fixed := checks.corrected_date(spec, cell["value"], cited)):
                            cell["was"], cell["value"] = cell["value"], fixed
                            cell["check"] = checks.check_value(spec, fixed, cited, True)
                entry["fields"] = {k: c for k, c in entry.get("fields", {}).items() if c.get("check") != "empty"}
