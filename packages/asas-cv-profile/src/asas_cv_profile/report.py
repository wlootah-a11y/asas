"""What a run leaves on disk for one CV: ``text.txt`` (the numbered lines),
``profile.json`` (everything, the raw model answers included) and ``review.md`` (what
needs a look first, then every field with its value, check and lines).
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .answers import cells as _cells
from .lines import Lines
from .other import UNCITED_RUN

#: what each entry flag asks of the reader
FLAGS = {"merged": "may be merged", "twin": "check the pairing"}


def write_outputs(built: Mapping[str, Any], folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "text.txt").write_text(Lines(built["text"]["lines"], built["text"].get("reader", "")).numbered() + "\n", encoding="utf-8")
    (folder / "profile.json").write_text(json.dumps(built, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (folder / "review.md").write_text(review(built), encoding="utf-8")


def _quote(lines: list[str], nums: list[int], limit: int = 160) -> str:
    q = " / ".join(lines[n - 1] for n in nums if 1 <= n <= len(lines))
    return (q[:limit] + "…") if len(q) > limit else q


def review(built: Mapping[str, Any]) -> str:
    """Every field with its value, its check and the lines it came from, for one read."""
    lines = built["text"]["lines"]
    t = built["text"]
    out = [f"# {built['file']}", "",
           f"Read by **{t['reader']}**, {len(lines)} lines, {t['pages']} pages.",
           f"Checks: {', '.join(f'{k} {v}' for k, v in sorted(built['checks'].items()))}."]
    fx = built.get("fixes") or {}
    if fx.get("sections_own_call"):
        out.append(f"Long sections read in calls of their own, beside the main call: {', '.join(fx['sections_own_call'])}.")
    if fx.get("sections_asked_again"):
        out.append(f"Asked again with their own lines, the one call having left them empty: {', '.join(fx['sections_asked_again'])}; "
                   f"found the second time: {', '.join(fx.get('sections_found_again') or []) or 'none'}.")
    if any(fx.get(k) for k in ("lines_asked", "skills_copied", "skills_restored")):
        out.append(f"Fixed in code or by a second call: {fx.get('lines_fixed', 0)} of {fx.get('lines_asked', 0)} parts with bad lines re-cited, "
                   f"{fx.get('skills_copied', 0)} skills reworded by the model set back to the CV's words, {fx.get('skills_restored', 0)} skills the model left out added from skill lists"
                   + (f" ({fx['skills_listed']} items in the CV's skills section, all taken by code)." if fx.get("skills_listed") else "."))
    if fx.get("other_repeats"):
        out.append("Left out of Other information, as repeats of lines the profile holds: " + "; ".join(
            f"lines {d['lines'][0]}-{d['lines'][-1]} (repeat lines {d['repeats'][0]}-{d['repeats'][-1]})" for d in fx["other_repeats"]) + ".")
    out.append("")
    for w in t["warnings"]:
        out.append(f"> {w}")
    oi = built.get("other_information") or {}
    if oi.get("lines"):
        out.append(f"> Other information: {oi['lines']} lines under {oi['topics']} headings that no field takes, kept as other information.")
    gaps = built.get("uncited") or []
    if gaps:
        out += ["", "## Lines no entry cites", "",
                f"Runs of {UNCITED_RUN} or more content lines that nothing in the profile cites: something missed, or what the profile has no field for.", ""]
        for first, last, count in gaps:
            out.append(f"- L{first}–L{last} ({count} lines): _{_quote(lines, [first], 100)}_")
    errors = [c for c in built["calls"] if c["error"]]
    for c in errors:
        out.append(f"> Call {c['group']} failed: {c['error']}")
    flagged = [(cat, sub, i, label, cell) for cat, sub, i, label, cell in _cells(built["profile"]) if cell["check"] in ("unverified", "bad_lines")]
    merged = [(cat, sub, i, f) for cat, subs in built["profile"].items() for sub, node in subs.items() if isinstance(node, list)
              for i, e in enumerate(node) for f in e.get("flags") or []]
    if flagged or merged:
        out += ["", "## Look at these first", ""]
    for cat, sub, i, f in merged:
        out.append(f"- {cat} > {sub} #{i + 1}: {f['message']}")
    if flagged:
        for cat, sub, i, label, cell in flagged:
            out.append(f"- {cat} > {sub} #{i + 1} > {label}: `{cell['value']}` ({cell['check']})")
    for cat, subs in built["profile"].items():
        out += ["", f"## {cat}"]
        for sub, node in subs.items():
            for i, entry in enumerate(node if isinstance(node, list) else [node]):
                head = f"### {sub}" + (f" #{i + 1}" if isinstance(node, list) else "") + "".join(f" ({FLAGS.get(f['code'], 'check')})" for f in entry.get("flags") or [])
                src = entry.get("lines") or []
                out += ["", head + (f"  (lines {', '.join(map(str, src))})" if src else "")]
                if src:
                    out.append(f"_{_quote(lines, src, 240)}_")
                out += ["", "| Field | Value | Check |", "|---|---|---|"]
                for label, cell in entry["fields"].items():
                    v = cell["value"]
                    v = "; ".join(map(str, v)) if isinstance(v, list) else str(v)
                    v = v.replace("\n", " / ").replace("|", "\\|")
                    if len(v) > 200:
                        v = v[:200] + "…"
                    if cell.get("was"):
                        v += f" (model wrote: {cell['was']})"
                    elif cell.get("restored"):
                        v += " (added from the skill list)"
                    out.append(f"| {label} | {v} | {cell['check']} |")
    out += ["", "## Calls", "", "| Group | Properties | Seconds | Tokens in / out | Error |", "|---|---|---|---|---|"]
    for c in built["calls"]:
        u = c["usage"] or {}
        out.append(f"| {c['group']} | {c['properties']} | {c['seconds']} | {u.get('input_tokens', 0)} / {u.get('output_tokens', 0)} | {c['error'] or ''} |")
    return "\n".join(out) + "\n"


def summary(rows: Sequence[Mapping[str, Any]]) -> str:
    """One line per CV of a run: how it was read and how its values checked."""
    out = ["# CV profile run", "", "| CV | Read by | Lines | Verified | Normalised | Inferred | Unverified | Failed calls | Seconds |",
           "|---|---|---|---|---|---|---|---|---|"]
    total: Counter = Counter()
    for b in rows:
        if "refused" in b:
            out.append(f"| {b['file']} | refused: {b['refused']} | | | | | | | |")
            continue
        if "failed" in b:  # an error outside the model calls; a later run reads it again
            out.append(f"| {b['file']} | failed: {b['failed']} | | | | | | | |")
            continue
        c = Counter(b["checks"])
        total.update(c)
        failed = sum(1 for x in b["calls"] if x["error"])
        secs = max((x["seconds"] for x in b["calls"]), default=0)
        out.append(f"| {b['file']} | {b['text']['reader']} | {len(b['text']['lines'])} | {c['verified']} | {c['normalised']} | {c['inferred']} | "
                   f"{c['unverified'] + c['bad_lines'] + c['merged_entry']} | {failed or ''} | {secs} |")
    filled = sum(total.values()) or 1
    out += ["", (f"All CVs: {sum(total.values()) - total['merged_entry']} values; verified {total['verified'] * 100 // filled}%, "
                 f"unverified or bad lines {(total['unverified'] + total['bad_lines']) * 100 // filled}%; entries that may be merged: {total['merged_entry']}.")]
    return "\n".join(out) + "\n"
