"""One entry per item, whatever the language: a bilingual CV's twins folded into one.

A bilingual CV states each role twice, in Arabic and in English, and the model copies
both as written (one CV held its 3 roles as 6, and its degree as 2); a long CV can
repeat one role outright. Two entries of one list are twins when

- one is in Arabic and the other in Latin script, and they keep the same years of every
  date, the same "current" and, for education, the same level: what a translation
  keeps, though the names change; or
- they are in the same script and their names and dates read the same: a plain repeat
  ("district I" and "district II" are two).

Matching is record linkage with blocking: each entry's keys are worked out once, the
entries are indexed by them, and each entry, in the CV's order, takes the first later
translation no earlier entry took, with every repeat of either. Translations pair one to
one, so three roles stated twice stay three; repeats read the same, so all of them go.
One pass leaves nothing a second would fold. The Latin one is kept, with every copy's
lines, any field it lacks, and each Arabic name under ``arabic`` beside its own.

Dates cannot tell two items apart when they share them: where more than one item in
either language has a pair's dates, the pair was chosen by CV order alone, and the kept
entry gets a ``twin`` flag for a person to check. An Arabic item and a different Latin
one that alone share their dates fold with no flag: telling them apart needs their
meaning, which no rule here reads.
"""

from __future__ import annotations

import re
from collections import defaultdict

from .answers import identity
from .arabic import ARABIC_RE
from .model import ProfileModel, Section

_LATIN = re.compile("[A-Za-z]")


def script(entry: dict, sub: Section) -> str:
    """"ar" or "latin": the script most of the entry's copied values are written in."""
    words = " ".join(str(entry["fields"][x.name]["value"]) for x in sub.asked
                     if x.kind == "copied" and x.name in entry["fields"] and entry["fields"][x.name].get("value"))
    return "ar" if len(ARABIC_RE.findall(words)) > len(_LATIN.findall(words)) else "latin"


def kept_in_translation(entry: dict, sub: Section) -> tuple:
    """What a translation keeps: the years of every date, whether it is current, and for
    education its level. Empty when the entry gives no date (and, for education, no level)."""
    f = entry["fields"]

    def got(x):
        return (f.get(x.name) or {}).get("value")

    years = tuple((x.name, str(got(x))[:4]) for x in sub.asked if x.type in ("date", "year") and got(x))
    extra = tuple((x.name, got(x)) for x in sub.asked if (x.type == "boolean" or x.role == "level") and got(x) not in (None, "Not stated"))
    return years + extra if years or (sub.kind == "education" and extra) else ()


def _fold(keep: dict, drop: dict, translated: bool) -> None:
    keep["lines"] = sorted(set(keep["lines"]) | set(drop["lines"]))
    for label, cell in drop["fields"].items():
        mine = keep["fields"].get(label)
        if mine is None:
            keep["fields"][label] = cell
        elif translated and cell.get("kind") == "copied" and cell.get("value") and cell.get("value") != mine.get("value"):
            mine["arabic"] = cell["value"]


def pair_twins(node: list[dict], sub: Section) -> tuple[list[dict], int]:
    """The list with each twin folded into its partner, and how many were folded."""
    keys = [(script(e, sub), identity(e, sub), kept_in_translation(e, sub)) for e in node]
    by_kept: dict[tuple, list[int]] = defaultdict(list)
    by_name: dict[tuple[str, str], list[int]] = defaultdict(list)
    items: dict[tuple, dict[str, set]] = defaultdict(lambda: defaultdict(set))  # the distinct items sharing each date key, by script
    for k, (s, ident, kept) in enumerate(keys):
        if kept:
            by_kept[kept].append(k)
            items[kept][s].add(ident or k)
        if ident:  # a repeat needs dates on the first of the two only
            by_name[(s, ident)].append(k)
    gone: set[int] = set()
    out = list(node)
    for i, (s, ident, kept) in enumerate(keys):
        if i in gone or not kept:
            continue
        t = next((j for j in by_kept[kept] if j > i and j not in gone and keys[j][0] != s), None)
        firsts = [i] if t is None else [i, t]
        copies = {j for k in firsts if keys[k][1] for j in by_name[keys[k][:2]] if j > i and j not in gone} | set(firsts)
        if len(copies) == 1:
            continue
        keep = t if t is not None and s == "ar" else i
        for j in sorted(copies - {keep}):  # in the CV's order
            _fold(out[keep], out[j], translated=keys[j][0] != keys[keep][0])
        if t is not None and any(len(v) > 1 for v in items[kept].values()):
            out[keep].setdefault("flags", []).append({"code": "twin", "message": (
                "Folded with its copy in the other language by dates that other entries share: check the two are the same.")})
        out[i] = out[keep]
        gone |= copies - {i}
    return [e for k, e in enumerate(out) if k not in gone], len(gone)


def merge_twins(profile: dict, model: ProfileModel) -> int:
    """Twins folded in every list section whose kind allows it, in place; returns how many."""
    by_name = {s.name: s for s in model.asked}
    folded = 0
    for nodes in profile.values():
        for name, node in nodes.items():
            sub = by_name.get(name)
            if sub is None or not sub.trait("twins") or not isinstance(node, list) or len(node) < 2:
                continue
            nodes[name], n = pair_twins(node, sub)
            folded += n
    return folded
