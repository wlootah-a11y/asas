"""Where each value stands in the CV: exact character spans on the lines it cites.

Source grounding, as Google's LangExtract does it: every value the profile copies from
the CV is mapped to the exact characters it came from, ``spans: [[line, start, end],
...]`` with ``start`` and ``end`` character offsets on that line (end exclusive). A text
field copies whole lines, so its spans are those lines. A copied value is found in the
lines it cites, exactly (case aside) or, failing that, by RapidFuzz's
``partial_ratio_alignment`` at ``SPAN_SCORE`` or more. A value the model wrote in its own
words (a date, a choice from a list, a number) has no span: those characters are not the
CV's, and the value's check already says how it relates to them.

Spans are worked out again on every build and recheck, never kept from an older run.
Other information reads them to keep exactly the part of a line no value covers.
"""

from __future__ import annotations

from rapidfuzz import fuzz

from .lines import Lines

#: RapidFuzz alignment score a copied value must reach to be placed where it was not found exactly
SPAN_SCORE = 90

Span = list[int]  # [line, start, end]


def find(value: str, nums: list[int], text: Lines) -> Span | None:
    """Where ``value`` stands on one of the lines ``nums``: the first exact match (case
    aside), else the best RapidFuzz alignment at SPAN_SCORE or more; None when neither."""
    wanted = value.strip()
    if not wanted:
        return None
    folded = wanted.lower()
    best: tuple[float, Span] | None = None
    for n in nums:
        line = text.line(n)
        if not line:
            continue
        lower = line.lower()
        if len(lower) == len(line) and (i := lower.find(folded)) >= 0:
            return [n, i, i + len(wanted)]
        if len(wanted) > len(line):  # the alignment would place the line in the value, not the value on the line
            continue
        hit = fuzz.partial_ratio_alignment(wanted, line, score_cutoff=SPAN_SCORE)
        if hit is not None and (best is None or hit.score > best[0]):
            best = (hit.score, [n, hit.dest_start, hit.dest_end])
    return best[1] if best else None


def ground(profile: dict, text: Lines) -> int:
    """Every cell's spans, in place: whole lines for a text field, the value's place for a
    copied one (each item of a copied list), none for a value in the model's own words.
    Returns how many cells are grounded."""
    grounded = 0
    for subs in profile.values():
        for node in subs.values():
            for entry in node if isinstance(node, list) else [node]:
                entry_lines = list(entry.get("lines") or [])
                for cell in (entry.get("fields") or {}).values():
                    cell.pop("spans", None)
                    kind, value = cell.get("kind"), cell.get("value")
                    nums = list(cell.get("lines") or entry_lines)
                    spans: list[Span] = []
                    if kind == "text":
                        spans = [[n, 0, len(line)] for n in nums if (line := text.line(n))]
                    elif kind == "copied":
                        for item in value if isinstance(value, list) else [value]:
                            if isinstance(item, str) and (span := find(item, nums, text)):
                                spans.append(span)
                    if spans:
                        cell["spans"] = spans
                        grounded += 1
    return grounded


def covered(profile: dict, skip: tuple[str, str] | None = None) -> dict[int, list[tuple[int, int]]]:
    """Each line's covered character ranges, merged, from every cell's spans; ``skip`` a
    (group, section) left out (other information, which is made from the rest)."""
    ranges: dict[int, list[tuple[int, int]]] = {}
    for cat, subs in profile.items():
        for sub, node in subs.items():
            if (cat, sub) == skip:
                continue
            for entry in node if isinstance(node, list) else [node]:
                for cell in (entry.get("fields") or {}).values():
                    for n, a, b in cell.get("spans") or []:
                        ranges.setdefault(n, []).append((a, b))
    return {n: _merge(r) for n, r in ranges.items()}


def gaps(length: int, ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """The parts of a line of ``length`` characters that ``ranges`` (merged) leave uncovered."""
    out, at = [], 0
    for a, b in ranges:
        if a > at:
            out.append((at, a))
        at = max(at, b)
    if at < length:
        out.append((at, length))
    return out


def _merge(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for a, b in sorted(ranges):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out
