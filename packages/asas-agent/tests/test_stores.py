"""Release stores: a store moves the bytes and `fetch_release` decides what they are.
Settings join the release only when an agent names them, so older ids hold; an edit
made in a store's own editor gets its own id; a tampered file is refused; the Langfuse
store keeps the whole agent in one prompt and finds a release by its label."""

import json
from types import SimpleNamespace

import pytest
import yaml
from conftest import run
from test_releases import FIXTURE_ID, build

from asas_agent import (
    FileStore,
    LangfuseStore,
    build_release,
    fetch_release,
    release_from,
)
from asas_agent.cli import main


def test_settings_join_the_release_and_its_id_only_when_named(agents_dir, tmp_path):
    assert build(agents_dir)["id"] == FIXTURE_ID
    agents = tmp_path / "agents"
    (agents / "helper").mkdir(parents=True)
    (agents / "skills").mkdir()
    (agents / "helper" / "prompt.md").write_text("Help.")
    definition = {"name": "Helper", "max_turns": 4, "max_tools": 4}
    (agents / "helper" / "agent.yaml").write_text(yaml.safe_dump(definition))
    plain = build_release(agents, "m-1", agent="helper")
    definition["settings"] = {"model": {"temperature": 0.2}}
    (agents / "helper" / "agent.yaml").write_text(yaml.safe_dump(definition))
    tuned = build_release(agents, "m-1", agent="helper")
    assert "settings" not in plain and tuned["settings"]["model"]["temperature"] == 0.2
    assert plain["id"] != tuned["id"]


def test_release_from_checks_the_shape_and_computes_the_id(agents_dir):
    release = build(agents_dir)
    body = {k: v for k, v in release.items() if k != "id"}
    assert release_from(body) == release
    edited = release_from({**body, "instructions": "Edited in the store."})
    assert edited["id"] != release["id"]
    with pytest.raises(ValueError, match="integrity"):
        release_from({**release, "max_tools": 999})
    with pytest.raises(ValueError, match="missing model"):
        release_from({k: v for k, v in body.items() if k != "model"})
    with pytest.raises(ValueError, match="unknown keys"):
        release_from({**body, "tools": []})
    with pytest.raises(ValueError, match="model configuration"):
        release_from(body, model="m-2")


def test_a_file_store_publishes_promotes_and_serves_a_label(agents_dir, tmp_path):
    store = FileStore(tmp_path / "releases")
    release = build(agents_dir)
    run(store.publish("helper", release))
    run(store.publish("helper", release))  # again: nothing changes
    with pytest.raises(FileNotFoundError):
        run(fetch_release(store, "helper", "production"))
    run(store.promote("helper", release["id"], "production"))
    assert run(fetch_release(store, "helper", "production", model="m-1")) == release
    (tmp_path / "releases" / f"{release['id']}.json").write_text(
        json.dumps({**release, "max_tools": 999})
    )
    with pytest.raises(ValueError, match="integrity"):
        run(fetch_release(store, "helper", "production"))


def test_a_file_store_never_reaches_outside_its_directory(agents_dir, tmp_path):
    store = FileStore(tmp_path / "releases")
    with pytest.raises(ValueError, match="Invalid agent release"):
        run(store.read("../etc", "production"))
    with pytest.raises(ValueError, match="Invalid agent release"):
        run(store.promote("helper", "../../x", "production"))
    (tmp_path / "releases" / "helper").mkdir(parents=True)
    (tmp_path / "releases" / "helper" / "production.json").write_text(
        json.dumps({"id": "../../etc/passwd"})
    )
    with pytest.raises(ValueError, match="Invalid agent release reference"):
        run(store.read("helper", "production"))
    with pytest.raises(ValueError, match="not published"):
        run(store.promote("helper", "0" * 64, "production"))


class FakeLangfuse:
    """The slice of the Langfuse 2.x client the store calls: versions with labels."""

    def __init__(self):
        self.versions = []  # (name, prompt, config, labels)

    def get_prompt(self, name, *, label, type, cache_ttl_seconds):
        for number, (n, prompt, config, labels) in enumerate(self.versions, 1):
            if n == name and label in labels:
                return SimpleNamespace(
                    name=n, prompt=prompt, config=config, version=number
                )
        raise LookupError("not found")

    def create_prompt(self, *, name, prompt, config, labels, type, commit_message):
        self.versions.append((name, prompt, config, list(labels)))

    def update_prompt(self, *, name, version, new_labels):
        for entry in self.versions:
            for label in new_labels:
                if label in entry[3]:
                    entry[3].remove(label)
        self.versions[version - 1][3].extend(new_labels)


def test_the_langfuse_store_keeps_the_agent_in_one_prompt(agents_dir):
    client = FakeLangfuse()
    store = LangfuseStore(client, prefix="agent-")
    release = build(agents_dir)
    run(store.publish("helper", release))
    run(store.publish("helper", release))
    assert len(client.versions) == 1
    name, prompt, config, labels = client.versions[0]
    assert name == "agent-helper" and prompt == release["instructions"]
    assert "id" not in config and "instructions" not in config
    assert labels == [f"release-{release['id'][:16]}"]
    run(store.promote("helper", release["id"], "production"))
    served = run(fetch_release(store, "helper", "production", model="m-1"))
    assert {k: v for k, v in served.items() if k != "source"} == release
    # An edit in Langfuse's editor is a new version; once labelled, it runs as itself.
    client.versions.append(("agent-helper", "Edited.", config, ["production"]))
    client.versions[0][3].remove("production")
    edited = run(fetch_release(store, "helper", "production"))
    assert edited["instructions"] == "Edited." and edited["id"] != release["id"]
    with pytest.raises(ValueError, match="not published"):
        run(store.promote("helper", "f" * 64, "production"))


def test_the_langfuse_store_keeps_where_it_read_from_outside_the_id(agents_dir):
    client = FakeLangfuse()
    store = LangfuseStore(client, prefix="agent-")
    release = build(agents_dir)
    run(store.publish("helper", release))
    run(store.promote("helper", release["id"], "production"))
    served = run(store.read("helper", "production"))
    assert served["source"] == {
        "name": "agent-helper",
        "version": 1,
        "label": "production",
    }
    fetched = run(fetch_release(store, "helper", "production", model="m-1"))
    assert fetched["id"] == release["id"]  # source is provenance, not content
    assert fetched["source"] == served["source"]
    # What a store read is published again without it, and a config cannot forge it.
    run(store.publish("elsewhere", fetched))
    assert "source" not in client.versions[-1][2]
    client.versions[0][2]["source"] = {"name": "forged", "version": 99}
    assert run(store.read("helper", "production"))["source"]["name"] == "agent-helper"
    # A release built from files has none.
    assert "source" not in release


def test_publish_and_promote_from_the_command_line(agents_dir, tmp_path, capsys):
    store = f"file:{tmp_path / 'releases'}"
    common = ["--agent", "helper", "--store", store]
    assert main(["publish", str(agents_dir), "--model", "m-1", *common]) == 0
    release_id = capsys.readouterr().out.strip()
    assert release_id == build_release(agents_dir, "m-1", agent="helper")["id"]
    assert main(["promote", "--id", release_id, "--label", "qa", *common]) == 0
    served = run(fetch_release(FileStore(tmp_path / "releases"), "helper", "qa"))
    assert served["id"] == release_id
