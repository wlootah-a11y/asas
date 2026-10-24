"""Which prompt, model and trace made an output, and what makes it stale."""

import asyncio

from asas_llm import provenance
from asas_llm.prompts import Prompt
from asas_llm.provenance import PromptRef
from conftest import run


def test_a_registry_prompt_is_named_by_version_and_a_local_copy_is_not():
    served = Prompt(name="parse-cv", type="text", content="x", version=12, source="langfuse")
    ref = provenance.ref_of(served, label="production")
    assert (ref.source, ref.version, ref.label) == ("langfuse", 12, "production")
    assert ref.sha and len(ref.sha) == 16 and ref.from_registry

    local = Prompt(name="parse-cv", type="text", content="x", version=12, source="local")
    fallback = provenance.ref_of(local)
    # The audit-trail rule: a copy the registry did not serve names no version.
    assert fallback.version is None and fallback.source == "local" and not fallback.from_registry
    assert fallback.sha == ref.sha  # same text, same digest


def test_identity_follows_the_version_for_a_registry_prompt_and_the_text_otherwise():
    ref = PromptRef(name="p", source="langfuse", version=3, sha="aaaa")
    same_text_new_version = PromptRef(name="p", source="langfuse", version=4, sha="aaaa")
    assert provenance.identity(ref, "m") != provenance.identity(same_text_new_version, "m")
    assert provenance.identity(ref, "m") == provenance.identity(PromptRef(name="p", source="langfuse", version=3, sha="bbbb"), "m")
    assert provenance.identity(ref, "m") != provenance.identity(ref, "other-model")
    local = PromptRef(name="p", source="local", sha="aaaa")
    assert provenance.identity(local, "m") != provenance.identity(PromptRef(name="p", source="local", sha="bbbb"), "m")
    assert provenance.identity(local, "m") != provenance.identity(ref, "m")
    assert provenance.identity(None, "m")["prompt"] == {"name": None}


def test_of_calls_digests_the_identity():
    out = provenance.of_calls([], {"scorer": {"prompt": {"name": "p", "version": 1}, "model": "m"}})
    assert out.identity == provenance.text_digest({"scorer": {"prompt": {"name": "p", "version": 1}, "model": "m"}})
    assert out.calls == []


def test_recording_collects_calls_made_in_tasks_and_nested_blocks_see_their_own():
    async def scenario():
        async def call(name):
            provenance.record(ref=PromptRef(name="p", source="langfuse", version=2), model="m", trace_id=name, call=name)

        with provenance.recording() as outer:
            await asyncio.gather(call("A"), asyncio.create_task(call("B")))
            with provenance.recording() as inner:
                await call("C")
        return outer, inner

    outer, inner = run(scenario())
    assert sorted(c.call for c in outer) == ["A", "B"]
    assert [c.call for c in inner] == ["C"]
    assert outer[0].prompt_version == 2 and outer[0].prompt_source == "langfuse"


def test_outside_a_recording_nothing_is_kept():
    provenance.record(model="m", trace_id="t", call="c")
    with provenance.recording() as calls:
        pass
    assert calls == []


def test_a_langchain_template_carries_its_ref_in_metadata():
    class Template:
        metadata = {provenance.PROMPT_REF_KEY: {"name": "p", "source": "repo", "sha": "abcd"}}

    ref = provenance.prompt_ref(Template())
    assert ref.name == "p" and ref.source == "repo" and ref.version is None
    assert provenance.prompt_ref(object()) is None
    with provenance.recording() as calls:
        provenance.record(prompt=Template(), model="m", trace_id="t", call="c")
    assert calls[0].prompt_sha == "abcd"


def test_repo_and_langfuse_refs_for_hand_built_templates():
    class Client:
        version = 7

        def get_langchain_prompt(self):
            return "text"

    ref = provenance.langfuse_ref("p", Client(), "production")
    assert (ref.version, ref.label, ref.source) == (7, "production", "langfuse")
    assert provenance.repo_ref("p", "text").version is None
