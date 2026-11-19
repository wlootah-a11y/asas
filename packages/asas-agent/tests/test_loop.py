"""The SDK loop: a skill is a playbook, activation swaps the instructions and the tools
on the next step and verifies nothing; the host's renderer gives the record and the
words; the ask handler returns the person's answer or ends the run at the question; a
failed run keeps its tool trace."""

import json

import pytest
from agents import Model
from agents.items import ModelResponse
from agents.usage import Usage
from conftest import run
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)

from asas_agent import (
    AskPending,
    ContextRenderer,
    SkillAware,
    ToolBudgetExceeded,
    described_tools,
    failure_tool_trace,
    instructions_for,
    run_agent,
)


class ScriptedModel(Model):
    def __init__(self, script):
        self.script = iter(script)
        self.seen = []
        self.inputs = []

    async def get_response(
        self, system_instructions, input, model_settings, tools, *a, **k
    ):
        self.seen.append((system_instructions, {t.name: t.description for t in tools}))
        self.inputs.append(input)
        name, payload = next(self.script)
        if name:
            output = ResponseFunctionToolCall(
                id=f"fc-{len(self.seen)}",
                call_id=f"call-{len(self.seen)}",
                name=name,
                arguments=json.dumps(payload),
                type="function_call",
            )
        else:
            output = ResponseOutputMessage(
                id="message",
                role="assistant",
                type="message",
                status="completed",
                content=[
                    ResponseOutputText(text=payload, annotations=[], type="output_text")
                ],
            )
        return ModelResponse(output=[output], usage=Usage(), response_id=None)

    async def stream_response(self, *args, **kwargs):
        raise NotImplementedError
        yield


class TicketRenderer:
    """A host whose record is a support ticket and whose person is a customer."""

    always = frozenset({"ask_customer", "ticket_read"})
    ask_tool = "ask_customer"
    activate_skill = "Start a skill from the catalogue."
    no_skill = "No skill is active. Activate the one the customer's message asks for."

    def head(self, snapshot):
        return [f"## Customer\n{snapshot.get('customer') or 'unknown'}"]

    def tail(self, snapshot):
        return ["## The ticket\nThe latest copy in the input is current."]

    def record(self, result):
        data = result.get("data") if isinstance(result, dict) else None
        value = data.get("rendered") if isinstance(data, dict) else None
        return value if isinstance(value, str) and value.startswith("Ticket ") else None

    def opening(self, record):
        return [
            {
                "role": "developer",
                "content": "## The ticket now\n" + (record or "None yet."),
            }
        ]


RENDERER = TicketRenderer()


def snapshot():
    return {
        "release": {
            "name": "Support",
            "instructions": "Base instructions",
            "model": "unused",
            "skills": {
                "triage": {
                    "description": "Triage",
                    "instructions": "Triage instructions",
                    "tools": ["ticket_update"],
                    "clauses_text": "- C-1: refunds over 100 go to a person",
                },
                "close": {
                    "description": "Close",
                    "instructions": "Close instructions",
                    "tools": ["ticket_close"],
                },
            },
            "max_turns": 8,
            "max_tools": 8,
        },
        "record": "Ticket T-1, open, revision 1.",
        "customer": "Name: Lina",
        "history": [{"role": "user", "content": "My parcel is late"}],
        "tools": [
            {
                "name": n,
                "description": "Tool",
                "schema": {"type": "object", "properties": {}},
            }
            for n in ("ticket_read", "ticket_update", "ticket_close", "ask_customer")
        ],
    }


async def never_asked(call_id, arguments):
    raise AssertionError("the ask tool was not expected")


async def never_dispatched(*args):
    raise AssertionError("no tool was expected")


def test_the_renderer_satisfies_the_seam():
    renderer: ContextRenderer = RENDERER
    assert renderer.ask_tool in renderer.always


def test_instructions_place_the_hosts_sections_around_the_skills():
    text = instructions_for(snapshot(), "triage", RENDERER)
    order = [
        "Base instructions",
        "## Customer\nName: Lina",
        "## Skills\n- triage: Triage\n- close: Close",
        "## Active skill\nSkill triage is active. Follow these instructions.\n\nTriage instructions",
        "## The policy clauses this skill applies\n- C-1: refunds over 100 go to a person",
        "## The ticket\nThe latest copy in the input is current.",
    ]
    assert text == "\n\n".join(order)
    assert "## Active skill\n" + RENDERER.no_skill in instructions_for(
        snapshot(), None, RENDERER
    )


def test_described_tools_name_their_skills_except_the_ones_every_skill_holds():
    tools = {d["name"]: d["description"] for d in described_tools(snapshot(), RENDERER)}
    assert list(tools)[0] == "activate_skill"
    assert tools["activate_skill"] == RENDERER.activate_skill
    assert tools["ticket_update"] == "[triage] Tool"
    assert tools["ticket_read"] == "Tool" and tools["ask_customer"] == "Tool"


def skill_less(tools=()):
    snap = snapshot()
    snap["release"]["skills"] = {}
    snap["tools"] = [d for d in snap["tools"] if d["name"] in tools]
    return snap


def test_a_release_with_no_skill_is_offered_no_activate_skill():
    tools = described_tools(skill_less({"ticket_read"}), RENDERER)
    assert [d["name"] for d in tools] == ["ticket_read"]
    assert described_tools(skill_less(), RENDERER) == []


def test_a_release_with_no_skill_renders_no_catalogue_and_no_active_skill():
    text = instructions_for(skill_less(), None, RENDERER)
    assert "## Skills" not in text and "## Active skill" not in text
    assert RENDERER.no_skill not in text
    assert text == "\n\n".join(
        [
            "Base instructions",
            "## Customer\nName: Lina",
            "## The ticket\nThe latest copy in the input is current.",
        ]
    )


def test_a_one_shot_release_calls_the_model_with_no_tool():
    model = ScriptedModel([(None, "Done.")])
    result = run(
        run_agent(
            skill_less(), never_dispatched, never_asked, renderer=RENDERER, model=model
        )
    )
    assert model.seen[0][1] == {}
    assert result["ended"] == "answered"


def test_activation_swaps_the_skill_and_the_record_follows_each_write():
    model = ScriptedModel(
        [
            ("activate_skill", {"name": "triage"}),
            ("ticket_update", {"late": True}),
            (None, "  I have marked it late.  "),
        ]
    )
    calls = []

    async def dispatch(call_id, name, args, active):
        calls.append((call_id, name, args, active))
        return {
            "ok": True,
            "data": {"revision": 2, "rendered": "Ticket T-1, late, revision 2."},
        }

    result = run(
        run_agent(snapshot(), dispatch, never_asked, renderer=RENDERER, model=model)
    )
    assert calls == [("call-2", "ticket_update", {"late": True}, "triage")]
    assert "Triage instructions" not in model.seen[0][0]
    assert (
        "Triage instructions" in model.seen[1][0]
        and model.seen[1][0] == model.seen[2][0]
    )
    assert model.inputs[0][0] == {
        "role": "developer",
        "content": "## The ticket now\nTicket T-1, open, revision 1.",
    }
    assert result["answer"] == "I have marked it late."
    assert result["ended"] == "answered"
    assert result["active_skill"] == "triage"
    assert result["record"] == "Ticket T-1, late, revision 2."
    assert [
        (t["name"], t["skill_before"], t["skill_after"], t["version"])
        for t in result["tool_trace"]
    ] == [
        ("activate_skill", None, "triage", None),
        ("ticket_update", "triage", "triage", 2),
    ]
    assert result["usage"]["model_calls"] == 3


def test_a_tool_outside_the_active_skill_is_denied_with_the_skill_to_activate():
    model = ScriptedModel([("ticket_close", {}), (None, "Done")])

    async def dispatch(*args):
        raise AssertionError("a denied tool is never dispatched")

    result = run(
        run_agent(snapshot(), dispatch, never_asked, renderer=RENDERER, model=model)
    )
    assert result["tool_trace"][0]["code"] == "denied"
    reply = json.loads(model.inputs[1][-1]["output"])
    assert reply == {
        "ok": False,
        "code": "denied",
        "message": "ticket_close is not a tool of the active skill; activate close first.",
    }


def test_the_ask_tool_goes_to_the_handler_and_its_answer_to_the_model():
    model = ScriptedModel(
        [("ask_customer", {"question": "Which parcel?"}), (None, "Thanks")]
    )
    asked = []

    async def ask(call_id, arguments):
        asked.append((call_id, arguments))
        return {"ok": True, "received": [{"from": "customer", "text": "The blue one"}]}

    async def dispatch(*args):
        raise AssertionError("the ask tool is never dispatched")

    result = run(run_agent(snapshot(), dispatch, ask, renderer=RENDERER, model=model))
    assert asked == [("call-1", {"question": "Which parcel?"})]
    assert "The blue one" in model.inputs[1][-1]["output"]
    assert result["ended"] == "answered"


def test_a_run_nobody_waits_for_ends_at_the_question():
    model = ScriptedModel([("ask_customer", {"question": "Which parcel?"})])

    async def ask(call_id, arguments):
        raise AskPending()

    result = run(
        run_agent(snapshot(), never_dispatched, ask, renderer=RENDERER, model=model)
    )
    assert result["ended"] == "asked"
    assert result["answer"] == "" and result["history"] == []
    assert [(t["name"], t["code"]) for t in result["tool_trace"]] == [
        ("ask_customer", "asked")
    ]


def test_a_failed_tool_is_a_typed_refusal_and_an_unknown_skill_lists_the_catalogue():
    model = ScriptedModel(
        [("activate_skill", {"name": "refunds"}), ("ticket_read", {}), (None, "Sorry")]
    )

    async def dispatch(*args):
        raise ConnectionError("down")

    result = run(
        run_agent(snapshot(), dispatch, never_asked, renderer=RENDERER, model=model)
    )
    assert json.loads(model.inputs[1][-1]["output"]) == {
        "ok": False,
        "code": "unknown_skill",
        "skills": ["close", "triage"],
    }
    refusal = json.loads(model.inputs[2][-1]["output"])
    assert refusal["code"] == "tool_failed" and "ConnectionError" in refusal["message"]
    assert (
        result["record"] == "Ticket T-1, open, revision 1."
    )  # a refusal carries no record


def test_the_tool_budget_stops_the_run_and_keeps_its_trace():
    source = snapshot()
    source["release"]["max_tools"] = 2
    model = ScriptedModel([("ticket_read", {})] * 5)

    async def dispatch(*args):
        return {"ok": True}

    with pytest.raises(ToolBudgetExceeded) as raised:
        run(run_agent(source, dispatch, never_asked, renderer=RENDERER, model=model))
    assert [t["name"] for t in failure_tool_trace(raised.value)] == [
        "ticket_read",
        "ticket_read",
    ]
    assert failure_tool_trace(ValueError("not ours")) == []


def test_a_skill_aware_run_model_is_told_the_active_skill():
    class Referenced(ScriptedModel):
        def bind(self, active_skill):
            self.active_skill = active_skill

    model = Referenced([("activate_skill", {"name": "close"}), (None, "Closed")])
    assert isinstance(model, SkillAware)

    async def dispatch(*args):
        return {"ok": True}

    run(
        run_agent(snapshot(), dispatch, never_asked, renderer=RENDERER, run_model=model)
    )
    assert model.active_skill() == "close"
