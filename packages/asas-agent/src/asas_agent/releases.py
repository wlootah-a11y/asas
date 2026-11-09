"""Releases: an agent's definition captured whole, under the hash of its content.

A run pins the release it started on and never rereads the files, so a retry or a
replay sees the same instructions and skills. The layout of an agents directory:

    <directory>/<agent>/agent.yaml     name, max_turns, max_tools, settings (optional)
    <directory>/<agent>/prompt.md      the instructions
    <directory>/<agent>/<extra files>  joined to the instructions, in the order given
    <directory>/skills/*/SKILL.md      the skills (asas_agent.skills)
    <directory>/releases/              published releases and the active pointers

The id is the SHA-256 of the release without its id, as `json.dumps(..., sort_keys=True)`
writes it. Any key added to a release changes every id, so the shape below is a contract.
`settings` is the one optional key: a release whose agent.yaml names none carries none, so
its id is what it was before settings existed.

`source` (0.5.0) says where a store read the release from (for Langfuse, the prompt's
`name`, `version` and `label`) so a run can link its generations to that version. It
is provenance, not content: it never enters the id, a file-built release has none, and
the same content read from another version has the same id.

`settings` is the rest of the agent as configuration: `model`, the SDK's model settings
(temperature, reasoning, max_tokens and the like), and `tracing`, `enabled` and
`sensitive` (whether the trace keeps the run's content).

A release lives in a store (:class:`~asas_agent.seams.ReleaseStore`): files here
(:class:`FileStore`), Langfuse (:mod:`asas_agent.langfuse_store`) or the host's own.
:func:`fetch_release` reads one through a label and verifies it, whatever the store.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from .seams import ReleaseStore
from .skills import load_skills

#: The keys every release carries; `settings` is the only optional one (and `source`,
#: which a store adds and the id ignores).
REQUIRED = ("name", "model", "instructions", "skills", "max_turns", "max_tools")
_ID = re.compile(r"[0-9a-f]{64}")
_NAME = re.compile(r"[a-z0-9][a-z0-9_.-]{0,99}")


def release_id(release: Mapping[str, Any]) -> str:
    """The content hash of a release that does not yet carry its id. `source`, where
    a store read it from, is provenance and is left out."""
    content = {k: v for k, v in release.items() if k != "source"}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def build_release(
    directory: Path, model: str, *, agent: str, extra_files: Sequence[str] = ()
) -> dict[str, Any]:
    """The release of `agent` as the files under `directory` define it today, for
    `model`. Each of `extra_files` that exists in the agent's folder joins the prompt,
    and its words are part of the hash."""
    folder = directory / agent
    definition = yaml.safe_load((folder / "agent.yaml").read_text())
    skills, always = load_skills(directory / "skills")
    instructions = (folder / "prompt.md").read_text()
    for name in extra_files:
        extra = folder / name
        if extra.exists():
            instructions = instructions.rstrip() + "\n\n" + extra.read_text()
    release: dict[str, Any] = {
        "name": definition["name"],
        "model": model,
        "instructions": "\n\n".join([instructions.rstrip(), *always]),
        "skills": skills,
        "max_turns": definition["max_turns"],
        "max_tools": definition["max_tools"],
    }
    if definition.get("settings"):
        release["settings"] = definition["settings"]
    release["id"] = release_id(release)
    return release


def release_from(
    payload: Mapping[str, Any], *, model: str | None = None
) -> dict[str, Any]:
    """A release from what a store returned, its id computed from its content.

    The shape is checked, never trusted. A payload that claims an id must hash to it
    (a file store pins content by id); one that claims none (an edit made in a store's
    own editor) gets the id of what it says now, so a run records what really ran.
    With `model`, a release built for another model is refused."""
    missing = [key for key in REQUIRED if key not in payload]
    if missing:
        raise ValueError(f"Agent release is missing {', '.join(missing)}")
    unknown = set(payload) - {*REQUIRED, "settings", "id", "source"}
    if unknown:
        raise ValueError(f"Agent release has unknown keys {', '.join(sorted(unknown))}")
    if not isinstance(payload["skills"], dict) or not all(
        isinstance(payload[key], int) and payload[key] > 0
        for key in ("max_turns", "max_tools")
    ):
        raise ValueError("Agent release skills or budgets are malformed")
    if "settings" in payload and not isinstance(payload["settings"], dict):
        raise ValueError("Agent release settings must be a mapping")
    if "source" in payload and not isinstance(payload["source"], dict):
        raise ValueError("Agent release source must be a mapping")
    release = {key: payload[key] for key in payload if key != "id"}
    release["id"] = release_id(release)
    if "id" in payload and payload["id"] != release["id"]:
        raise ValueError("Agent release integrity mismatch")
    if model is not None and release["model"] != model:
        raise ValueError("Agent release model configuration mismatch")
    return release


async def fetch_release(
    store: ReleaseStore, agent: str, label: str, *, model: str | None = None
) -> dict[str, Any]:
    """The release `label` points at for `agent` in `store`, verified
    (:func:`release_from`). The store moves the bytes; this decides what they are."""
    return release_from(await store.read(agent, label), model=model)


def _checked(value: str, pattern: re.Pattern, what: str) -> str:
    if not pattern.fullmatch(value):
        raise ValueError(f"Invalid agent release {what}")
    return value


class FileStore:
    """Releases as files: `<directory>/<id>.json` holds a release whole, and
    `<directory>/<agent>/<label>.json` is the label, `{"id": ...}`.

    Agent names and labels are lower-case slugs and ids are 64 hex characters, so no
    value read from a pointer or passed in reaches outside the directory."""

    def __init__(self, directory: Path):
        self.directory = directory

    async def read(self, agent: str, label: str) -> dict[str, Any]:
        pointer = (
            self.directory
            / _checked(agent, _NAME, "agent")
            / f"{_checked(label, _NAME, 'label')}.json"
        )
        pinned = _checked(json.loads(pointer.read_text())["id"], _ID, "reference")
        return json.loads((self.directory / f"{pinned}.json").read_text())

    async def publish(self, agent: str, release: Mapping[str, Any]) -> None:
        _checked(agent, _NAME, "agent")
        path = self.directory / f"{_checked(release['id'], _ID, 'reference')}.json"
        if not path.exists():
            self.directory.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(release, sort_keys=True, ensure_ascii=False))

    async def promote(self, agent: str, release_id: str, label: str) -> None:
        if not (
            self.directory / f"{_checked(release_id, _ID, 'reference')}.json"
        ).exists():
            raise ValueError("Agent release is not published")
        folder = self.directory / _checked(agent, _NAME, "agent")
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{_checked(label, _NAME, 'label')}.json").write_text(
            json.dumps({"id": release_id})
        )


def read_release(directory: Path, model: str, *, environment: str) -> dict[str, Any]:
    """The release published as active for `environment`, refused unless its content
    still hashes to its id and it was built for `model`."""
    pointer = json.loads(
        (directory / "releases" / f"active-{environment}.json").read_text()
    )
    pinned = pointer["id"]
    if len(pinned) != 64 or any(c not in "0123456789abcdef" for c in pinned):
        raise ValueError("Invalid agent release reference")
    release = json.loads((directory / "releases" / f"{pinned}.json").read_text())
    check = {k: v for k, v in release.items() if k != "id"}
    if release_id(check) != pinned or release["model"] != model:
        raise ValueError("Agent release integrity or model configuration mismatch")
    return release


def load_release(
    model: str,
    directory: Path,
    *,
    agent: str,
    extra_files: Sequence[str] = (),
    environment: str = "local",
    built_in: Collection[str] = ("local", "test"),
) -> dict[str, Any]:
    """The release a run pins: built from the files in the environments of `built_in`,
    read from the published release everywhere else."""
    if environment not in built_in:
        return read_release(directory, model, environment=environment)
    return build_release(directory, model, agent=agent, extra_files=extra_files)


def stage_view(
    release: Mapping[str, Any], stage: str, *, default_stage: str = "intake"
) -> dict:
    """The release as a run of one stage reads it: only the skills of that stage, a
    skill naming its stage in its header (`stage:`) and one that names none being
    `default_stage`'s. The id stays the release's: the view is derived."""
    return {
        **release,
        "skills": {
            name: skill
            for name, skill in release["skills"].items()
            if skill.get("stage", default_stage) == stage
        },
    }


def stage_tools(
    release: Mapping[str, Any], stage: str, *, default_stage: str = "intake"
) -> frozenset[str]:
    """The tools a stage's skills hold: the release's tool list for that stage."""
    return frozenset(
        tool
        for skill in stage_view(release, stage, default_stage=default_stage)[
            "skills"
        ].values()
        for tool in skill["tools"]
    )
