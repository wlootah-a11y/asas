"""The profile model: the host's sections and fields, and what the code knows of each.

A host describes its profile in one YAML file. Every section and field keeps the
host's own name, so the profile comes back in the host's words; a section may name
the **kind** it is (work history, education, skills...) and a field the **role** it
plays in it (a start date, an employer), which is all the code needs to know:

.. code-block:: yaml

    name: Candidate profile
    region: AE                      # where a phone number with no country code is from (optional)
    sections:
      - name: Roles                 # the host's name, unique
        group: Work experience      # where the profile nests it (optional)
        kind: work                  # one of KINDS (optional)
        list: true                  # one entry per item
        fields:
          - {name: Job title, type: string}
          - {name: Employer, type: string, role: employer}
          - {name: Start date, type: date, role: start}
          - {name: End date, type: date, role: end}
          - {name: Current, type: boolean, role: current}
          - {name: Seniority, type: enum, values: [Junior, Senior], infer: true}
          - {name: Months, type: number, computed: duration_months}

Types: ``string``, ``url``, ``phone`` (compared as numbers, a number with no country
code read in the model's ``region``, an ISO 3166 code; with none, only numbers with
their country code are compared), ``text`` (cited as lines
and copied from them by code), ``date`` (YYYY-MM or YYYY), ``year``, ``number``,
``boolean``, ``enum`` and ``multi_enum`` (from ``values``; without ``values``, the
CV's own words). ``note`` is shown to the model beside the field. ``infer: true``
lets the value be worked out from the CV rather than copied; ``required: true`` on an
``enum`` with ``values`` makes the choice non-nullable in the schema, so every entry
gets one (strict structured outputs enforce it, where a note only asks); ``copied: false`` marks
a string chosen from a list the host keeps (a country code) rather than copied.
``computed`` fields are filled after the model answers, never asked: the package
fills those named in :data:`COMPUTED`, and ``computed: host`` is left to the host.

Strict structured outputs take at most 100 object properties per schema on Azure
OpenAI (learn.microsoft.com, structured outputs), so :func:`pack` keeps each call
under :data:`BUDGET`.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, create_model

#: properties per call; Azure's documented cap is 100
BUDGET = 90

TYPES = ("string", "url", "phone", "text", "date", "year", "number", "boolean", "enum", "multi_enum")

#: what each kind of section is to the code. ``dates``: the date ranges (``range``)
#: or single dates (``one``) its entries state, read to flag two items read as one.
#: ``own_call``: a long list at the back of a CV, read in calls of its own on a long
#: CV. ``second_look``: a list the lines no entry took may belong in. ``twins``: a
#: bilingual CV's two copies of one item are folded into one.
KINDS: dict[str, dict[str, Any]] = {
    "work": {"dates": "range", "second_look": True},
    "work_level": {"twins": False},
    "education": {"dates": "range", "second_look": True},
    "scholarships": {"second_look": True},
    "courses": {"dates": "one", "own_call": True, "second_look": True},
    "certifications": {"own_call": True, "second_look": True},
    "licences": {"second_look": True},
    "career_breaks": {"dates": "range", "second_look": True},
    "projects": {"dates": "range", "own_call": True, "second_look": True},
    "publications": {"own_call": True, "second_look": True},
    "patents": {"own_call": True, "second_look": True},
    "talks": {"own_call": True, "second_look": True},
    "awards": {"own_call": True, "second_look": True},
    "memberships": {"dates": "range", "own_call": True, "second_look": True},
    "volunteering": {"dates": "range", "own_call": True, "second_look": True},
    "student_activities": {"dates": "range", "second_look": True},
    "skills": {"twins": False},
    "languages": {"twins": False},
    "other_information": {"twins": False},
}

#: the roles a field may play, by the kind of its section
ROLES: dict[str, tuple[str, ...]] = {
    "work": ("start", "end", "current", "employer", "skills_used"),
    "education": ("institution", "level", "graduation"),
    "skills": ("name",),
    "other_information": ("topic", "details", "kind"),
}

#: the fields the package fills after the model answers (``computed:``), by section kind
#: (None: any section): a role's months and how precise its start is, the months of
#: work with overlaps counted once and the months between, and how long and how lately
#: each skill was used in the roles that name it
COMPUTED: dict[str, str | None] = {
    "duration_months": "work",
    "date_precision": "work",
    "total_months": None,
    "gap_months": None,
    "months_used": "skills",
    "last_used": "skills",
    "host": None,
}


class ModelError(ValueError):
    """The profile model file is not one this package can use; the message says where."""


@dataclass
class Spec:
    """One field of a section."""

    section: str
    name: str
    type: str
    note: str = ""
    values: list[str] = field(default_factory=list)
    infer: bool = False
    copied: bool = True
    role: str | None = None
    computed: str | None = None
    required: bool = False  # an enum with values the schema never lets be null
    slug: str = ""
    region: str | None = None  # a phone field's: the model's region

    @property
    def may_infer(self) -> bool:
        """The value may be worked out from the CV, not only copied."""
        return self.infer

    @property
    def kind(self) -> str:
        """How the value relates to the CV text, which decides how it is checked."""
        if self.type == "text":
            return "text"  # returned as line numbers; code copies the lines
        if self.type in ("string", "url", "phone") and self.copied:
            return "copied"
        return "normalised"  # dates, numbers, choices, booleans: the CV's words, rewritten


@dataclass
class Section:
    """One section of the profile: the host's name, the group it nests under, its kind."""

    group: str
    name: str
    specs: list[Spec]
    repeating: bool = False
    kind: str | None = None
    headings: tuple[str, ...] = ()

    @property
    def slug(self) -> str:
        return slugify(f"{self.group} {self.name}")

    @property
    def asked(self) -> list[Spec]:
        """The fields the model fills: every one not computed. Other information is made
        by code from the lines no field took, so the model fills none of it."""
        return [] if self.kind == "other_information" else [s for s in self.specs if s.computed is None]

    def trait(self, name: str) -> Any:
        return KINDS.get(self.kind or "", {}).get(name, True if name == "twins" else None)

    def role(self, role: str) -> Spec | None:
        return next((s for s in self.specs if s.role == role), None)

    def computed(self, what: str) -> Spec | None:
        return next((s for s in self.specs if s.computed == what), None)


@dataclass
class ProfileModel:
    """The whole profile, as the host's YAML file describes it."""

    name: str
    sections: list[Section]
    region: str | None = None
    sha256: str = ""
    source: str = ""

    @property
    def asked(self) -> list[Section]:
        """The sections with at least one field the model fills, in file order."""
        return [s for s in self.sections if s.asked]

    def of_kind(self, kind: str) -> list[Section]:
        return [s for s in self.sections if s.kind == kind]

    def first(self, kind: str) -> Section | None:
        return next(iter(self.of_kind(kind)), None)

    def where(self, computed: str) -> list[tuple[Section, Spec]]:
        return [(s, f) for s in self.sections for f in s.specs if f.computed == computed]


def slugify(text: str) -> str:
    s = re.sub(r"[^0-9a-zA-Z]+", "_", text.lower()).strip("_")
    return s if not s[:1].isdigit() else f"f_{s}"


def load_model(path: Path) -> ProfileModel:
    """The profile model in a YAML file, checked: unknown types, kinds, roles and
    computed names, and duplicate section names, are refused with where they are."""
    import yaml

    raw = path.read_bytes()
    try:
        doc = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ModelError(f"{path.name}: not YAML ({exc})") from None
    model = parse_model(doc, where=path.name)
    model.sha256 = hashlib.sha256(raw).hexdigest()
    model.source = path.name
    return model


def parse_model(doc: Any, *, where: str = "profile model") -> ProfileModel:
    if not isinstance(doc, dict) or not isinstance(doc.get("sections"), list):
        raise ModelError(f"{where}: expected a mapping with a list of sections")
    region = _region(doc.get("region"), where)
    sections: list[Section] = []
    seen: set[str] = set()
    for i, s in enumerate(doc["sections"], 1):
        at = f"{where}, section {i}"
        if not isinstance(s, dict) or not s.get("name") or not isinstance(s.get("fields"), list):
            raise ModelError(f"{at}: needs a name and a list of fields")
        name = str(s["name"])
        if name in seen:
            raise ModelError(f"{at}: the name {name!r} is used twice")
        seen.add(name)
        kind = s.get("kind")
        if kind is not None and kind not in KINDS:
            raise ModelError(f"{at} ({name}): unknown kind {kind!r}; one of {', '.join(KINDS)}")
        if s.get("headings") and kind is None:
            raise ModelError(f"{at} ({name}): headings need a kind, the words name sections of that kind")
        specs = [_spec(f, name, kind, f"{at} ({name}), field {j}") for j, f in enumerate(s["fields"], 1)]
        if s.get("list") and (whole := [f.name for f in specs if f.computed in ("total_months", "gap_months")]):
            raise ModelError(f"{at} ({name}): {', '.join(whole)} sum every role, so it needs a section that is not a list")
        taken: set[str] = set()
        for spec in specs:
            slug = slugify(spec.name)
            while slug in taken or slug == "lines":
                slug += "_"
            taken.add(slug)
            spec.slug = slug
        sections.append(Section(group=str(s.get("group") or ""), name=name, specs=specs, repeating=bool(s.get("list", False)),
                                kind=kind, headings=tuple(str(h) for h in s.get("headings") or ())))
    for sec in sections:
        for spec in sec.specs:
            if spec.type == "phone":
                spec.region = region
    return ProfileModel(name=str(doc.get("name") or ""), sections=sections, region=region)


def _region(value: Any, where: str) -> str | None:
    """The model's region, checked against the regions libphonenumber knows."""
    if value is None:
        return None
    import phonenumbers

    code = str(value).upper()
    if code not in phonenumbers.SUPPORTED_REGIONS:
        raise ModelError(f"{where}: unknown region {value!r}; an ISO 3166 code libphonenumber knows, such as AE")
    return code


def _spec(f: Any, section: str, kind: str | None, at: str) -> Spec:
    if not isinstance(f, dict) or not f.get("name"):
        raise ModelError(f"{at}: needs a name")
    typ = f.get("type", "string")
    if typ not in TYPES:
        raise ModelError(f"{at}: unknown type {typ!r}; one of {', '.join(TYPES)}")
    role, computed = f.get("role"), f.get("computed")
    if role is not None and role not in ROLES.get(kind or "", ()):
        raise ModelError(f"{at}: role {role!r} is not one a {kind or 'plain'} section has ({', '.join(ROLES.get(kind or '', ())) or 'none'})")
    if computed is not None:
        if computed not in COMPUTED:
            raise ModelError(f"{at}: unknown computed {computed!r}; one of {', '.join(COMPUTED)}")
        if COMPUTED[computed] not in (None, kind):
            raise ModelError(f"{at}: computed {computed!r} belongs in a {COMPUTED[computed]} section")
    values = [str(v) for v in f.get("values") or []]
    required = bool(f.get("required", False))
    if required and not (typ == "enum" and values):
        raise ModelError(f"{at}: required needs an enum with values, a choice the model can always make")
    return Spec(section=section, name=str(f["name"]), type=typ, note=str(f.get("note") or ""), values=values, required=required,
                infer=bool(f.get("infer", False)), copied=bool(f.get("copied", True)), role=role, computed=computed)


# ---------------------------------------------------------------- typed models
#: lines as inclusive ranges, [first, last], one line as [n, n] (SmartResume's index
#: ranges): a ten-line description is two numbers, and no extra schema properties,
#: since an array of arrays has none
Ranges = list[list[int]]


def _annotation(spec: Spec) -> tuple[Any, str]:
    """The field's type and description; its note is in the description whatever the type."""
    ann, desc = _typed(spec)
    if spec.note and spec.note not in desc:
        desc = f"{desc} {spec.note}"
    return ann, desc


def _typed(spec: Spec) -> tuple[Any, str]:
    note = spec.note
    if spec.type == "text":
        return Ranges, "The lines that hold this text, as ranges [first, last], in reading order. Numbers, not words. Empty when absent."
    if spec.type in ("date", "year"):
        fmt = "YYYY" if spec.type == "year" else "YYYY-MM when the month is stated, else YYYY"
        return Optional[str], f"{fmt}. Null when not stated."
    if spec.type == "number":
        return Optional[float], "A number. Null when not stated."
    if spec.type == "boolean":
        return Optional[bool], "True or false. Null when the CV does not say."
    if spec.type in ("enum", "multi_enum") and spec.values:
        lit: Any = Literal[tuple(spec.values)]  # a type built at run time from the host's values
        if spec.type == "multi_enum":
            return list[lit], "Any of the listed values. Empty when none."
        if spec.required:
            return lit, "One of the listed values, always: never null."
        return Optional[lit], "One of the listed values. Null when not stated."
    if spec.type == "multi_enum":
        return list[str], f"Each as the CV words it ({note}). Empty when none."
    if note:
        return Optional[str], f"As the CV words it ({note}). Null when not stated."
    return Optional[str], "As the CV words it. Null when not stated."


def entry_model(section: Section) -> type[BaseModel]:
    fields: dict[str, Any] = {
        spec.slug: (ann, Field(None if ann is not list else ..., title=spec.name, description=desc))
        for spec in section.asked
        for ann, desc in [_annotation(spec)]
    }
    fields["lines"] = (Ranges, Field(..., description="Every line this entry was taken from, as ranges [first, last]."))
    model = create_model(section.slug, **fields)  # type: ignore[call-overload]
    model.__doc__ = f"{section.group} > {section.name}" if section.group else section.name
    return model


def group_model(name: str, sections: list[Section]) -> type[BaseModel]:
    fields: dict[str, Any] = {}
    for section in sections:
        entry: Any = entry_model(section)
        title = f"{section.group} > {section.name}" if section.group else section.name
        if section.repeating:
            fields[section.slug] = (list[entry], Field(..., description=f"{title}: one entry per item. Empty when the CV has none."))
        else:
            fields[section.slug] = (Optional[entry], Field(None, description=f"{title}. Null when the CV has none of it."))
    return create_model(name, **fields)  # type: ignore[call-overload]


def strict_schema(model: type[BaseModel]) -> dict:
    """The schema as the Agents SDK sends it under ``strict``: what the provider counts."""
    from agents import AgentOutputSchema

    return AgentOutputSchema(model, strict_json_schema=True).json_schema()


def count_properties(schema: dict) -> int:
    """Object properties, counting a referenced definition once per use (the safe reading)."""
    defs = schema.get("$defs", {})

    def walk(node: Any) -> int:
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(defs[node["$ref"].split("/")[-1]])
            n = len(node.get("properties", {}))
            return n + sum(walk(v) for k, v in node.items() if k not in ("$defs",))
        if isinstance(node, list):
            return sum(walk(v) for v in node)
        return 0

    return walk({k: v for k, v in schema.items() if k != "$defs"})


@dataclass
class Group:
    """Sections filled by one call, and the typed answer that call returns."""

    name: str
    subs: list[Section]
    model: type[BaseModel]
    properties: int
    second_look: bool = False  # its entries may name items the list already holds, which are folded in


def group(name: str, sections: list[Section]) -> Group:
    model = group_model(name, sections)
    return Group(name, sections, model, count_properties(strict_schema(model)))


def pack(sections: list[Section], budget: int = BUDGET) -> list[Group]:
    """Sections in file order, packed into calls that each stay under ``budget``.
    A section is never split; neighbours are kept together where they fit. Each section
    is counted once: a group's count is the sum of its sections', since
    :func:`count_properties` counts a shared definition at every use."""
    groups: list[list[Section]] = []
    current: list[Section] = []
    size = 0
    for section in sections:
        n = count_properties(strict_schema(group_model("Trial", [section])))
        if current and size + n > budget:
            groups.append(current)
            current, size = [section], n
        else:
            current.append(section)
            size += n
    if current:
        groups.append(current)
    return [group(f"ProfileGroup{i}", members) for i, members in enumerate(groups, 1)]


def one_group(sections: list[Section]) -> Group:
    """Every section in one schema: one call reads the whole CV."""
    return group("ProfileAll", sections)
