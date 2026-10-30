"""The one call a host makes: a CV file in, the numbered lines and the cited profile out.

    builder = ProfileBuilder(model, asker, pdf=LayoutReader(endpoint, key))
    built = await builder.build(data, "cv.pdf")
    built["text"]     # the numbered lines and how they were read
    built["profile"]  # group > section > entry > field, each with its value, lines and check

The stages, each in its own module:

1. :mod:`.lines`: the file becomes numbered lines, with no model.
2. :mod:`.calls`: one call fills every section the model fills; :mod:`.followups`
   adds the long lists' own calls, the sections asked again and the second look.
3. :mod:`.answers`: parts that cited lines the text lacks are asked again in one short
   call; every value is checked against its lines.
4. Code settles the rest: :mod:`.twins` folds a bilingual CV's twins, :mod:`.skills`
   keeps skills in the CV's words, :mod:`.experience` and the host's ``finish`` fill the
   computed fields, :mod:`.other` keeps the lines no field took, :mod:`.merged` flags
   entries that may be several.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections import Counter
from dataclasses import replace
from datetime import date

from . import __version__, answers, calls, experience, followups, skills, spans
from .headings import Headings, numbered_items
from .lines import Lines, read_cv
from .merged import flag_merged
from .model import Group, ProfileModel, group, one_group, pack
from .other import OtherLines
from .seams import Ask, CallResult, Finish, PdfReader
from .shapes import Built
from .twins import merge_twins

#: content lines under a list's heading that no entry cites, past which a capped build
#: sends that list to its one follow-up (a long list the main call read only in part)
LIST_LEFT = 2


def _call_row(r: CallResult, groups: dict[str, Group]) -> dict:
    g = groups.get(r.group)
    return {"group": r.group, "sections": [answers.title(s) for s in g.subs] if g else [], "properties": g.properties if g else 0,
            "seconds": r.seconds, "usage": r.usage, "error": r.error, "raw": r.output.model_dump() if r.output is not None else None}


def _checks(profile: dict, text: Lines, model: ProfileModel) -> dict:
    counts = Counter(cell["check"] for *_, cell in answers.cells(profile))
    if merged := flag_merged(profile, model, text):
        counts["merged_entry"] = merged
    return dict(counts)


class ProfileBuilder:
    """Builds profiles to one profile model (a facade over the stages). ``ask`` makes the
    model calls (:class:`~asas_cv_profile.calls.Asker`, or a test's stand-in); ``pdf``
    reads PDFs; ``finish`` is the host's last step; ``one_call=False`` packs the
    sections into calls under :data:`~asas_cv_profile.model.BUDGET` properties each.
    ``followups="one"`` (the default) caps a build at two calls: the main call reads the
    whole CV (no long list gets calls of its own), then one call carries every gap at once
    (the sections asked again and the second look), cited lines the text lacks are not
    asked again, and other information takes its kind from its heading instead of a call.
    ``followups="each"`` gives each gap a call of its own: a few more items on long CVs,
    up to about eleven calls."""

    def __init__(self, model: ProfileModel, ask: Ask | None, *, pdf: PdfReader | None = None, finish: Finish | None = None,
                 one_call: bool = True, followups: str = "one", keep_all: bool = False, keep_personal: bool = False):
        if followups not in ("each", "one"):
            raise ValueError(f"followups is 'each' or 'one', not {followups!r}")
        self.model, self.ask, self.pdf, self.finish, self.one_call = model, ask, pdf, finish, one_call
        self.followups = followups
        extra: dict[str, list[str]] = {}
        for s in model.sections:
            if s.kind and s.headings:
                extra.setdefault(s.kind, []).extend(s.headings)
        self.headings = Headings(extra)
        self.other = OtherLines(model, self.headings, keep_all=keep_all, keep_personal=keep_personal)

    @property
    def _ask(self) -> Ask:
        if self.ask is None:
            raise ValueError("this builder has no model to ask: build and other_again need one, recheck does not")
        return self.ask

    async def _fill(self, calls_: list[tuple[Group, str]]) -> list[CallResult]:
        return list(await asyncio.gather(*(calls.fill(self._ask, text, g) for g, text in calls_)))

    async def build(self, data: bytes, filename: str, *, as_of: date | None = None, text: Lines | None = None) -> Built:
        """``as_of`` is the day "present" is counted to: the application date, else today.
        ``text`` skips the reader: a saved text read again. Raises
        :class:`~asas_cv_profile.lines.Refused` for a file that is not a CV to read."""
        today = as_of or date.today()
        if text is None:  # the reader waits on a service: off the event loop, so other requests go on
            text = await asyncio.to_thread(read_cv, data, filename, pdf=self.pdf, headings=self.headings)
        numbered = text.numbered()
        capped = self.followups == "one"
        own = [] if capped else followups.own_calls(self.model, self.headings, text)
        taken = {s.name for g, _ in own for s in g.subs}
        rest = [s for s in self.model.asked if s.name not in taken]
        groups = [one_group(rest)] if self.one_call else pack(rest)
        results = await self._fill([(g, numbered) for g in groups] + [(g, text.part(nums)) for g, nums in own])
        groups += [g for g, _ in own]
        failed = any(r.error for r in results)
        again = [] if failed else followups.asked_again(self.model, self.headings, results, groups, text, taken)
        if again and not capped:
            results += await self._fill([(g, text.part(nums)) for g, nums in again])
            groups += [g for g, _ in again]
        # the answers so far, assembled once: the second look and a capped build's gaps both read them
        draft = None if failed else answers.assemble(results, groups, text, [])
        look = [] if draft is None else followups.second_look(self.model, self.other.uncited(draft, text), text)
        if capped:
            if (asked := await self._one_followup(draft, text, again, look)) is not None:
                results.append(asked[0])
                groups.append(asked[1])
        elif look:
            results += await self._fill([(g, text.part(nums)) for g, nums in look])
            groups += [g for g, _ in look]
        bad = answers.bad_refs(results, groups, text)
        fixed = 0
        if bad and not capped:
            res = await calls.fix_lines(self._ask, numbered, bad)
            fixed = answers.apply_fixes(results, groups, res.output, bad)
            results.append(res)

        warnings = list(text.warnings)
        profile = answers.assemble(results, groups, text, warnings)
        twins, skill_fixes, host_warnings = self._settle(profile, text, today)
        warnings += self._short_lists(profile, text) + host_warnings
        other = self.other.put(profile, text)
        repeats: list[dict] = []
        if capped:
            other = self.other.kinds_by_heading(profile)
        elif self.other.usable and self.other.to_name(profile) and not any(r.error for r in results):  # a failed run is read again later
            res, repeats = await self.other.name(profile, text, self._ask)
            results.append(res)
            other = self.other.counts(profile)

        by_name = {g.name: g for g in groups}
        found_again = [g.subs[0].name for g, _ in again
                       if any(r.group == g.name and r.output is not None and any(answers.items(r.output, x) for x in g.subs) for r in results)]
        release = (self.ask.release if self.ask else None) or {}
        return {
            "file": filename,
            "text": {"reader": text.reader, "pages": text.pages, "lines": text.lines,
                     "sha256": hashlib.sha256("\n".join(text.lines).encode()).hexdigest(), "warnings": warnings},
            "file_sha256": hashlib.sha256(data).hexdigest(),
            "profile_model": {"file": self.model.source, "sha256": self.model.sha256, "fields": sum(len(s.asked) for s in self.model.asked)},
            "profile": profile,
            "checks": _checks(profile, text, self.model),
            "uncited": self.other.uncited(profile, text),
            "other_information": other,
            "fixes": {"sections_own_call": sorted({g.subs[0].name for g, _ in own}), "chunk_calls": len(own),
                      "sections_asked_again": [g.subs[0].name for g, _ in again], "sections_found_again": found_again,
                      "second_look_calls": len(look), "second_look_lines": len({n for _, nums in look for n in nums}),
                      "second_look_found": sum(len(answers.items(r.output, x)) for g, _ in look for r in results
                                               if r.group == g.name and r.output is not None for x in g.subs),
                      "lines_asked": len(bad), "lines_fixed": fixed, "twins_merged": twins, "other_repeats": repeats,
                      **{f"skills_{k}": v for k, v in skill_fixes.items()}},
            "calls": [_call_row(r, by_name) for r in results],
            "package": f"asas-cv-profile {__version__}",  # the follow-up calls' wording is the package's: its version names it
            "model": release.get("model"),
            "release": release.get("id"),
            "as_of": today.isoformat(),
            "built_on": date.today().isoformat(),
        }

    def _gaps(self, draft: dict, text: Lines, again: list) -> dict[str, list[int]]:
        """Section name to the lines to read again: the sections asked again, and each list
        whose heading still has LIST_LEFT or more content lines no entry in ``draft`` cites
        (a long list the main call read only in part)."""
        where: dict[str, list[int]] = {}
        for g, nums in again:
            for s in g.subs:
                where.setdefault(s.name, []).extend(nums)
        cited = self.other.cited(draft)
        for kind, nums in self.headings.find(text.lines, other_ends=False).items():
            subs = [s for s in self.model.asked if s.kind == kind and s.trait("own_call")]
            left = [n for n in nums if n not in cited and self.headings.heading(text.line(n) or "") is None]
            if subs and len(left) >= LIST_LEFT:
                for s in subs:
                    where.setdefault(s.name, []).extend(left)
        return where

    def _followup_message(self, merged: Group, where: dict[str, list[int]], loose: list[int], text: Lines) -> str:
        """The follow-up's lines, and where in them each section's items are."""
        nums = sorted({n for ns in where.values() for n in ns} | set(loose))
        width = max(3, len(str(len(text.lines))))
        msg = calls.fill_message(text.part(nums), merged)
        if where:
            msg += " Where each one's items are: " + "; ".join(
                f"{name}: {calls.spans_label(sorted(set(ns)), width)}" for name, ns in where.items()) + "."
        if loose:
            msg += (f" Lines {calls.spans_label(loose, width)} were taken by no entry yet: put each item there in the "
                    "section it belongs to, and leave out what is not an item of these sections.")
        return msg

    async def _one_followup(self, draft: dict | None, text: Lines, again: list, look: list) -> tuple[CallResult, Group] | None:
        """The one follow-up of a capped build: the sections asked again, the half-read lists
        and the second look, in one call that says where each section's lines are. None when
        a call failed (``draft`` None: the run is read again later) or there is no gap."""
        if draft is None:
            return None
        where = self._gaps(draft, text, again)
        loose = sorted({n for _, nums in look for n in nums})
        named = {s.name: s for s in self.model.asked if s.name in where} | {s.name: s for g, _ in look for s in g.subs}
        if not named:
            return None
        order = {s.name: i for i, s in enumerate(self.model.asked)}
        # folded into items the list holds only for loose lines: a list's own uncited lines hold new items
        merged = replace(group("Followup", sorted(named.values(), key=lambda s: order[s.name])), second_look=bool(loose))
        msg = self._followup_message(merged, where, loose, text)
        return await self._ask(merged.name, msg, merged.model, f"{merged.properties} properties"), merged

    def _settle(self, profile: dict, text: Lines, today: date) -> tuple[int, dict[str, int], list[str]]:
        """What code settles once the answers are in, in one order for a build and a
        recheck: twins folded, skills kept in the CV's words, the package's computed fields,
        then the host's, then where each value stands in the CV (:mod:`.spans`). Returns the twins folded, the skill fixes and the host's warnings."""
        twins = merge_twins(profile, self.model)
        skill_fixes = skills.copy_and_restore(profile, text.lines, self.model, self.headings)
        experience.add_experience(profile, self.model, today)
        host = self.finish(profile, self.model, today) if self.finish else []
        spans.ground(profile, text)
        return twins, skill_fixes, host

    def _short_lists(self, profile: dict, text: Lines) -> list[str]:
        """A list whose heading numbers more items than the profile holds: some were not read."""
        out = []
        for kind, nums in self.headings.find(text.lines).items():
            expected = numbered_items(nums, text.lines)
            subs = [s for s in self.model.asked if s.kind == kind]
            if expected < 5 or not subs:
                continue
            nodes = [(profile.get(s.group) or {}).get(s.name) for s in subs]
            got = sum(len(n) if isinstance(n, list) else 1 for n in nodes if n is not None)
            if got < 0.9 * expected:
                out.append(f"{', '.join(s.name for s in subs)}: the profile holds {got} entries, the text numbers {expected} "
                           "under its heading. Some were not read.")
        return out

    async def other_again(self, built: Built) -> Built:
        """Other information made again from a saved profile, with a fresh kind call: the
        step alone, so a change to it needs no new reading of the CV."""
        text = Lines(built["text"]["lines"], built["text"]["reader"])
        profile = built["profile"]
        repeats: list[dict] = []
        if (t := self.other.target) is not None:
            s = t.section
            profile.get(s.group, {}).pop(s.name, None)
            spans.ground(profile, text)
            self.other.put(profile, text)
            built["calls"] = [c for c in built.get("calls", []) if c.get("group") != "OtherKinds"]
            if self.other.held(profile):
                res, repeats = await self.other.name(profile, text, self._ask)
                built["calls"].append(_call_row(res, {}))
        built.setdefault("fixes", {})["other_repeats"] = repeats
        built["other_information"] = self.other.counts(profile)
        merged = (built.get("checks") or {}).get("merged_entry")
        built["checks"] = dict(Counter(c["check"] for *_, c in answers.cells(profile)))
        if merged:
            built["checks"]["merged_entry"] = merged
        built["uncited"] = self.other.uncited(profile, text)
        return built

    def recheck(self, built: Built) -> Built:
        """The saved values checked again with the current rules: no model call. Done twice,
        it gives the same result as once."""
        text = Lines(built["text"]["lines"], built["text"]["reader"])
        profile = built["profile"]
        answers.recheck_values(profile, self.model.asked, text)
        for s in self.model.of_kind("skills"):  # skills added by code are added again under the current rules, never kept from an older run
            named = s.role("name")
            node = (profile.get(s.group) or {}).get(s.name)
            if named and isinstance(node, list):
                kept = [e for e in node if not (e["fields"].get(named.name) or {}).get("restored")]
                if kept:
                    profile[s.group][s.name] = kept
                else:
                    del profile[s.group][s.name]
        as_of = date.fromisoformat(built["as_of"]) if built.get("as_of") else date.today()
        twins, skill_fixes, host_warnings = self._settle(profile, text, as_of)
        fixes = built.setdefault("fixes", {})
        fixes["twins_merged"] = fixes.get("twins_merged", 0) + twins  # folded so far, not only now
        fixes["skills_listed"] = skill_fixes.get("listed", 0)
        built["text"]["warnings"] += [w for w in host_warnings if w not in built["text"]["warnings"]]
        repeat_lines = frozenset(n for d in built["fixes"].get("other_repeats") or [] for n in d["lines"])
        built["other_information"] = self.other.put(profile, text, repeat_lines)
        built["checks"] = _checks(profile, text, self.model)
        built["uncited"] = self.other.uncited(profile, text)
        built["text"]["warnings"] = [w for w in built["text"]["warnings"] if "personal facts" not in w]
        for cat, sub, _, label, cell in answers.cells(profile):
            if cell.get("kind") == "text" and isinstance(cell.get("value"), str) and answers.personal(cell["value"]):
                built["text"]["warnings"].append(f"{cat} > {sub} > {label} quotes lines with personal facts the profile must not hold "
                                                 "(marital status, religion, birth date or the like). Check the prompt.")
        return built
