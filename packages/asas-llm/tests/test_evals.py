"""One prompt replayed over cases through the production runner, checked,
judged and counted, pass^N."""

import json

from langchain_core.language_models.fake_chat_models import FakeListChatModel
from pydantic import BaseModel

from asas_llm import evals
from asas_llm.evals import EvalCase, Judge, cases_from_langfuse, expected, run_cases
from asas_llm.prompts import InMemoryPromptStore
from asas_llm.runners import LLMRunner, static_model
from asas_llm.tracing import NullTracer
from conftest import run


class Parsed(BaseModel):
    name: str
    years: int


def runner_with(responses):
    store = InMemoryPromptStore()
    store.text("parse-cv", "Read {{cv}}")
    store.text("eval-judge", "Judge {{answer}} against {{criteria}} for {{inputs}}")
    return LLMRunner(model_factory=static_model(FakeListChatModel(responses=list(responses))), prompts=store, tracer=NullTracer(), default_model="m")


def test_expected_reads_models_dicts_and_lists_and_a_missing_fact_fails():
    value = Parsed(name="Jane", years=8)
    assert expected(value, {"name": "Jane", "years": 8}) == {"name": True, "years": True}
    assert expected(value, {"years": 9, "city": "x"}) == {"years": "got 8", "city": "missing"}
    assert expected({"a": [{"b": 1}]}, {"a.0.b": 1}) == {"a.0.b": True}


def test_cases_pass_and_fail_on_their_expectations_with_trace_ids():
    runner = runner_with([json.dumps({"name": "Jane", "years": 8}), json.dumps({"name": "Ali", "years": 3})])
    cases = [
        EvalCase("jane", {"cv": "Jane, 8 years"}, expect={"years": 8}),
        EvalCase("ali", {"cv": "Ali, 3 years"}, expect={"years": 4}),
    ]
    report = run(run_cases(runner, "parse-cv", cases, output_model=Parsed, input_variable=None))
    assert report.passed == {"jane": True, "ali": False}
    assert report.pass_rate == 0.5
    assert report.failed_checks == {"years": 1}
    assert all(r.trace_id for r in report.results)
    assert report.to_dict()["results"][1]["checks"] == {"years": "got 3"}


def test_pass_n_needs_every_attempt_and_a_host_check_can_say_why():
    runner = runner_with([json.dumps({"name": "Jane", "years": 8}), json.dumps({"name": "Jane", "years": 7})])
    cases = [EvalCase("jane", {"cv": "x"}, check=lambda v: True if v.years == 8 else f"years was {v.years}")]
    report = run(run_cases(runner, "parse-cv", cases, output_model=Parsed, attempts=2, input_variable=None))
    assert report.passed == {"jane": False}
    assert [r.checks["check"] for r in report.results] == [True, "years was 7"]


def test_a_failed_call_is_a_failed_case_not_a_crash():
    runner = runner_with(["not json"])
    report = run(run_cases(runner, "parse-cv", [EvalCase("x", {"cv": "x"})], output_model=Parsed, input_variable=None))
    assert report.passed == {"x": False}
    assert "StructuredOutputError" in report.results[0].error


def test_the_judge_scores_against_the_criteria():
    runner = runner_with([
        json.dumps({"name": "Jane", "years": 8}),
        json.dumps({"score": 5, "reason": "complete"}),
        json.dumps({"name": "Ali", "years": 3}),
        json.dumps({"score": 2, "reason": "missed the degree"}),
    ])
    cases = [
        EvalCase("jane", {"cv": "x"}, criteria="every role is listed"),
        EvalCase("ali", {"cv": "y"}, criteria="the degree is read"),
    ]
    report = run(run_cases(runner, "parse-cv", cases, output_model=Parsed, judge=Judge(runner), input_variable=None))
    assert report.passed == {"jane": True, "ali": False}
    assert report.results[1].judge_score == 2 and "missed" in report.results[1].checks["judge"]


def test_cases_come_from_a_prompts_recent_traces():
    class Trace:
        def __init__(self, id, input):
            self.id, self.input = id, input

    class Page:
        def __init__(self, data):
            self.data = data

    class Client:
        def __init__(self):
            self.asked = []

        def fetch_traces(self, *, name, page, limit):
            self.asked.append((name, page, limit))
            if page == 1:
                return Page([Trace("t1", {"cv": "a"}), Trace("t2", "not a dict"), Trace("t3", {"cv": "b"})])
            return Page([])

    client = Client()
    cases = cases_from_langfuse(client, "parse-cv", limit=10, page_size=3)
    assert [(c.id, c.inputs, c.source) for c in cases] == [("t1", {"cv": "a"}, "langfuse:t1"), ("t3", {"cv": "b"}, "langfuse:t3")]
    assert client.asked[0] == ("parse-cv", 1, 3)
    assert evals.__all__  # the module is exported whole
