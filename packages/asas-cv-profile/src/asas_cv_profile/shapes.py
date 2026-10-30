"""The shape of what a run returns and writes to ``profile.json``, for a host's type
checker: plain dicts and lists, so it is JSON as it stands."""

from __future__ import annotations

from typing import Any, Literal, TypedDict

#: what a value's check says (see :mod:`asas_cv_profile.checks`); ``unplaced``: a line no field took
Check = Literal["verified", "normalised", "inferred", "unverified", "bad_lines", "computed", "unplaced"]


class Cell(TypedDict, total=False):
    value: Any
    kind: Literal["copied", "text", "normalised", "computed"]
    check: Check
    lines: list[int]  # a text field's own lines
    was: Any  # what the model wrote, when code set it back to the CV's words
    arabic: str  # a bilingual CV's Arabic copy of a name
    restored: bool  # a skill added from the CV's skills list


class Entry(TypedDict, total=False):
    lines: list[int]
    fields: dict[str, Cell]
    flags: list[dict]  # {"code": "merged", "message": ...}
    source: Literal["skills list", "elsewhere"]


#: group > section > one entry, or a list of entries for a ``list`` section
Profile = dict[str, dict[str, "Entry | list[Entry]"]]


class Built(TypedDict):
    file: str
    text: dict  # reader, pages, lines, sha256, warnings
    file_sha256: str
    profile_model: dict  # file, sha256, fields
    profile: Profile
    checks: dict[str, int]
    uncited: list[list[int]]  # [first, last, count] runs no entry cites
    other_information: dict  # lines, topics
    fixes: dict
    calls: list[dict]
    package: str  # "asas-cv-profile <version>": the follow-up calls' wording is the package's
    model: str | None
    release: str | None
    as_of: str
    built_on: str
