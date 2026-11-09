"""Releases in Langfuse: one Langfuse text prompt per agent, a version per release.

The prompt text is the release's instructions and the prompt's `config` is the rest
of it (name, model, skills, budgets, settings), so a person with access edits the
whole agent in Langfuse's own editor, and the next read of the label runs it. The id
is never stored in Langfuse: :func:`~asas_agent.releases.fetch_release` computes it
from what the label serves, so an edit made in the editor gets its own id and the run
records what really ran.

`read` also returns where it read the release from, `source` (the prompt's `name`,
`version` and the `label` asked for). It is not part of the id, and `run_agent` uses
it to link the run's generations to that prompt version in Langfuse.

A published release also carries the label `release-<first 16 of its id>`, which is
how publishing again finds it instead of adding a version, and how `promote` finds
the version to move a label to.

    from langfuse import Langfuse
    from asas_agent import LangfuseStore, fetch_release

    store = LangfuseStore(Langfuse(), prefix="agent-", cache_ttl_seconds=60)
    release = await fetch_release(store, "jd-builder", "production")

Who may move `production` is Langfuse's to enforce (its roles, or a project only a
release pipeline writes to); the store moves whatever label it is told to.

Needs the Langfuse 2.x client (`pip install 'asas-agent[langfuse]'`); ``import
asas_agent`` loads this module only when its name is first used. The client is
synchronous, so its calls run in a worker thread.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

#: The label that names a release's version, from the first 16 hex of its id.
RELEASE_LABEL = "release-{}"


class LangfuseStore:
    """A :class:`~asas_agent.seams.ReleaseStore` over a ``langfuse.Langfuse`` (2.x)
    client the host already has. `prefix` namespaces the prompt names; the SDK's own
    cache serves a label for `cache_ttl_seconds` and then refreshes it."""

    def __init__(
        self, client: Any, *, prefix: str = "agent-", cache_ttl_seconds: int = 60
    ):
        self.client = client
        self.prefix = prefix
        self.cache_ttl_seconds = cache_ttl_seconds

    def _get(self, agent: str, label: str, ttl: int):
        return self.client.get_prompt(
            self.prefix + agent, label=label, type="text", cache_ttl_seconds=ttl
        )

    async def read(self, agent: str, label: str) -> dict[str, Any]:
        prompt = await asyncio.to_thread(
            self._get, agent, label, self.cache_ttl_seconds
        )
        config = {
            k: v for k, v in (prompt.config or {}).items() if k not in ("id", "source")
        }
        source = {"name": prompt.name, "version": prompt.version, "label": label}
        return {**config, "instructions": prompt.prompt, "source": source}

    async def _version(self, agent: str, release_id: str) -> int | None:
        try:
            # Uncached: a version published a moment ago must be found.
            prompt = await asyncio.to_thread(
                self._get, agent, RELEASE_LABEL.format(release_id[:16]), 0
            )
        except Exception:  # noqa: BLE001 the 2.x client raises its API's NotFound
            return None
        return prompt.version

    async def publish(self, agent: str, release: Mapping[str, Any]) -> None:
        if await self._version(agent, release["id"]) is not None:
            return
        config = {
            k: v
            for k, v in release.items()
            if k not in ("id", "instructions", "source")
        }
        await asyncio.to_thread(
            self.client.create_prompt,
            name=self.prefix + agent,
            prompt=release["instructions"],
            config=config,
            labels=[RELEASE_LABEL.format(release["id"][:16])],
            type="text",
            commit_message=f"release {release['id']}",
        )

    async def promote(self, agent: str, release_id: str, label: str) -> None:
        version = await self._version(agent, release_id)
        if version is None:
            raise ValueError("Agent release is not published")
        await asyncio.to_thread(
            self.client.update_prompt,
            name=self.prefix + agent,
            version=version,
            # Kept beside the new label, whether the server adds or replaces labels.
            new_labels=[label, RELEASE_LABEL.format(release_id[:16])],
        )


__all__ = ["RELEASE_LABEL", "LangfuseStore"]
