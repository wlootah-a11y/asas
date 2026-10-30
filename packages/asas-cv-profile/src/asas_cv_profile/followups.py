"""The calls beside and after the first one, each sent only where the text shows a need.

- **Own calls.** On a CV over LONG_CV lines, each long list at the back (``own_call``
  kinds: publications, courses, awards...) of OWN_LINES lines or more goes to calls of
  its own, in chunks cut where items start: one call over a 659-line academic CV kept
  22 of its 101 publications. Roles, degrees, skills and languages stay in the main
  call, which reads the whole text: a two-column CV can carry the second page of its
  roles under another heading.
- **Asked again.** A list section with a heading in the text that came back empty is
  asked again with its own lines (a 16-page CV once came back with no publications at
  all); a ``work_level`` section left empty while roles were found is asked again with
  the roles' lines.
- **Second look.** Runs of lines no entry took are sent again with only the list
  sections that may hold them (``second_look`` kinds): a heading the reader missed or
  misnamed leaves real items there, such as 90 lines of research projects under two
  "papers" headings. Only uncited lines are sent, so nothing is read twice. It stays in
  small packed calls even when the main call is one: on 58 uncited lines of a real CV,
  one call over every list section found 0, 2 and 0 items in three runs, and the
  packed calls 15, 27 and 8.
"""

from __future__ import annotations

from dataclasses import replace

from .answers import items, spans
from .headings import Headings, chunks
from .lines import Lines
from .model import Group, ProfileModel, Section, group, pack, slugify
from .seams import CallResult

LONG_CV, OWN_LINES = 250, 40


def named(prefix: str, subs: list[Section], part: int | None = None) -> Group:
    """A follow-up call's group, named after its first section: ``AgainPublication``, ``OwnPublication2``."""
    return group(prefix + "".join(x.capitalize() for x in slugify(subs[0].name).split("_")) + (str(part) if part else ""), subs)


def _together(model: ProfileModel, kind: str) -> list[Section]:
    """The sections one heading of ``kind`` covers: every section of that kind the model fills."""
    return [s for s in model.asked if s.kind == kind]


def own_calls(model: ProfileModel, headings: Headings, text: Lines) -> list[tuple[Group, list[int]]]:
    if len(text.lines) <= LONG_CV:
        return []
    out = []
    for kind, nums in headings.find(text.lines, other_ends=False).items():
        subs = _together(model, kind)
        if len(nums) >= OWN_LINES and subs and all(s.trait("own_call") for s in subs):
            parts = chunks(nums, text.lines)
            out += [(named("Own", subs, k if len(parts) > 1 else None), part) for k, part in enumerate(parts, 1)]
    return out


def empty_lists(model: ProfileModel, headings: Headings, results: list[CallResult], groups: list[Group],
                text: Lines) -> list[tuple[list[Section], list[int]]]:
    """The list sections the text has a heading for but the answers left empty, with the lines under that heading."""
    by_name = {g.name: g for g in groups}
    answered = {sub.name: bool(items(r.output, sub)) for r in results if r.output is not None and r.group in by_name
                for sub in by_name[r.group].subs}
    out = []
    for kind, nums in headings.find(text.lines).items():
        subs = _together(model, kind)
        if subs and all(s.name in answered and not answered[s.name] and s.repeating for s in subs):
            out.append((subs, nums))
    return out


def asked_again(model: ProfileModel, headings: Headings, results: list[CallResult], groups: list[Group], text: Lines,
                taken: set[str]) -> list[tuple[Group, list[int]]]:
    again = [(named("Again", subs), nums) for subs, nums in empty_lists(model, headings, results, groups, text)
             if not {s.name for s in subs} & taken]
    if (level := missing_level(results, groups, text)):
        again.append(level)
    return again


def missing_level(results: list[CallResult], groups: list[Group], text: Lines) -> tuple[Group, list[int]] | None:
    by_name = {g.name: g for g in groups}
    roles, level, sub = [], False, None
    for r in results:
        if r.output is None or r.group not in by_name:
            continue
        for s in by_name[r.group].subs:
            for item in items(r.output, s):
                if s.kind == "work":
                    roles += spans(item.lines, len(text.lines))[0]
                elif s.kind == "work_level":
                    level = level or any(getattr(item, x.slug, None) not in (None, "", [], "Not stated") for x in s.asked)
            if s.kind == "work_level":
                sub = s
    if level or not roles or sub is None:
        return None
    return named("Again", [sub]), sorted(set(roles))


def second_look(model: ProfileModel, uncited: list[list[int]], text: Lines) -> list[tuple[Group, list[int]]]:
    nums = [n for first, last, _ in uncited for n in range(first, last + 1)]
    listed = [s for s in model.asked if s.repeating and s.trait("second_look")]
    if not nums or not listed:
        return []
    packs = pack(listed)
    return [(replace(group(f"Place{k}x{j}", packed.subs), second_look=True), part)
            for k, part in enumerate(chunks(nums, text.lines), 1) for j, packed in enumerate(packs, 1)]
