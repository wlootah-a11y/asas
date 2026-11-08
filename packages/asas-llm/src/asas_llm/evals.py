"""Single-call evals: one prompt replayed over recorded cases, checked and
judged, with a pass rate.

Most of the quality questions an AI product has are about one call: did
``parse-cv`` read this CV correctly, does ``candidate-job-scorer`` still give
this person 4 out of 5 after the prompt was edited. An agent-shaped eval (a
scripted conversation, as ``asas-agent`` runs) does not answer them; this
does, and it runs through the same :class:`~asas_llm.runners.LLMRunner` the
product uses, so the prompt store, the guard, the overrides and the trace are
the production ones.

    cases = [EvalCase("senior-dev", {"cv_markdown": md}, expect={"years_experience": 8})]
    report = await run_cases(runner, "parse-cv", cases, output_model=ParsedCV)
    report.pass_rate          # 0.0 to 1.0
    report.results[0].trace_id

A case passes when every check passes on every attempt. Checks:

* ``expect``: ``{"path.to.field": value}`` on the typed value (or the text),
  a missing field failing as surely as a wrong one;
* ``check``: a host function ``(value) -> bool | str`` (``str`` is the reason
  it failed);
* a :class:`Judge`, which asks a model whether the answer meets the case's
  ``criteria`` and scores it 0 to 5, through a judge prompt the host keeps in
  its store (``judge_prompt``), passing at or above ``threshold``.

``attempts`` above 1 is the pass^N rule: a case that passes three times of
four is not passing. Cases come from the host's own fixtures, or from the
tracing backend: :func:`cases_from_langfuse` turns a prompt's recent traces
into cases, since the runner records each call's inputs as the trace input.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from .errors import LLMError

log = logging.getLogger(__name__)

Check = Callable[[Any], bool | str | Awaitable[bool | str]]


@dataclass
class EvalCase:
    id: str
    inputs: dict[str, Any]
    expect: dict[str, Any] = field(default_factory=dict)
    check: Check | None = None
    criteria: str | None = None
    #: Where the case came from (a trace id, a fixture file); reported only.
    source: str | None = None


@dataclass
class CaseResult:
    id: str
    attempt: int
    passed: bool
    checks: dict[str, bool | str] = field(default_factory=dict)
    trace_id: str | None = None
    value: Any = None
    error: str | None = None
    judge_score: float | None = None
    judge_reason: str | None = None


@dataclass
class EvalReport:
    prompt: str
    results: list[CaseResult]
    attempts: int

    @property
    def cases(self) -> dict[str, list[CaseResult]]:
        by_case: dict[str, list[CaseResult]] = {}
        for r in self.results:
            by_case.setdefault(r.id, []).append(r)
        return by_case

    @property
    def passed(self) -> dict[str, bool]:
        """Per case: every attempt passed."""
        return {case: all(r.passed for r in runs) for case, runs in self.cases.items()}

    @property
    def pass_rate(self) -> float:
        passed = self.passed
        return sum(passed.values()) / len(passed) if passed else 0.0

    @property
    def failed_checks(self) -> dict[str, int]:
        """How often each check failed, across every attempt."""
        counts: dict[str, int] = {}
        for r in self.results:
            for name, ok in r.checks.items():
                if ok is not True:
                    counts[name] = counts.get(name, 0) + 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt": self.prompt,
            "attempts": self.attempts,
            "pass_rate": self.pass_rate,
            "passed": self.passed,
            "failed_checks": self.failed_checks,
            "results": [
                {
                    "id": r.id,
                    "attempt": r.attempt,
                    "passed": r.passed,
                    "checks": r.checks,
                    "trace_id": r.trace_id,
                    "error": r.error,
                    "judge_score": r.judge_score,
                    "judge_reason": r.judge_reason,
                }
                for r in self.results
            ],
        }


def _lookup(value: Any, path: str) -> tuple[bool, Any]:
    node = value
    for part in path.split("."):
        if isinstance(node, BaseModel):
            if not hasattr(node, part):
                return False, None
            node = getattr(node, part)
        elif isinstance(node, dict):
            if part not in node:
                return False, None
            node = node[part]
        elif isinstance(node, (list, tuple)) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return False, None
    return True, node


def expected(value: Any, expect: dict[str, Any]) -> dict[str, bool | str]:
    """Each ``{"path": value}`` against ``value``; a missing fact fails."""
    out: dict[str, bool | str] = {}
    for path, want in expect.items():
        found, got = _lookup(value, path)
        if not found:
            out[path] = "missing"
        elif got != want:
            out[path] = f"got {got!r}"
        else:
            out[path] = True
    return out


class JudgeVerdict(BaseModel):
    score: float
    reason: str


class Judge:
    """A model judging an answer against a case's criteria, through a prompt
    in the host's store with the variables ``criteria``, ``inputs`` and
    ``answer``, answering :class:`JudgeVerdict` (``score`` 0 to 5, ``reason``).
    Passes at or above ``threshold``."""

    def __init__(self, runner: Any, *, judge_prompt: str = "eval-judge", threshold: float = 4.0, model: str | None = None) -> None:
        self.runner = runner
        self.judge_prompt = judge_prompt
        self.threshold = threshold
        self.model = model

    async def judge(self, case: EvalCase, value: Any) -> JudgeVerdict:
        answer = value.model_dump_json() if hasattr(value, "model_dump_json") else json.dumps(value, ensure_ascii=False, default=str)
        result = await self.runner.ainvoke(
            self.judge_prompt,
            {
                "criteria": case.criteria or "",
                "inputs": json.dumps(case.inputs, ensure_ascii=False, default=str),
                "answer": answer,
            },
            output_model=JudgeVerdict,
            model=self.model,
            input_variable=None,
            name=f"eval-judge:{case.id}",
        )
        return result.value


async def run_cases(
    runner: Any,
    prompt: str,
    cases: Iterable[EvalCase],
    *,
    output_model: type[BaseModel] | None = None,
    judge: Judge | None = None,
    attempts: int = 1,
    concurrency: int = 1,
    **call_kwargs: Any,
) -> EvalReport:
    """Every case, ``attempts`` times each, through ``runner.ainvoke(prompt,
    case.inputs, output_model=...)``; ``call_kwargs`` reach the call."""
    cases = list(cases)
    gate = asyncio.Semaphore(max(1, concurrency))

    async def one(case: EvalCase, attempt: int) -> CaseResult:
        async with gate:
            try:
                got = await runner.ainvoke(prompt, case.inputs, output_model=output_model, **call_kwargs)
            except LLMError as exc:
                return CaseResult(id=case.id, attempt=attempt, passed=False, trace_id=exc.trace_id, error=f"{type(exc).__name__}: {exc}")
            checks: dict[str, bool | str] = expected(got.value, case.expect)
            if case.check is not None:
                verdict = case.check(got.value)
                if asyncio.iscoroutine(verdict):
                    verdict = await verdict
                checks["check"] = True if verdict is True else (verdict if isinstance(verdict, str) else "failed")
            score = reason = None
            if judge is not None and case.criteria:
                try:
                    judged = await judge.judge(case, got.value)
                    score, reason = judged.score, judged.reason
                    checks["judge"] = True if score >= judge.threshold else f"scored {score}: {reason}"
                except LLMError as exc:
                    checks["judge"] = f"judge failed: {type(exc).__name__}"
            return CaseResult(
                id=case.id,
                attempt=attempt,
                passed=all(v is True for v in checks.values()),
                checks=checks,
                trace_id=got.trace_id,
                value=got.value,
                judge_score=score,
                judge_reason=reason,
            )

    results = await asyncio.gather(*(one(case, n) for case in cases for n in range(1, max(1, attempts) + 1)))
    return EvalReport(prompt=prompt, results=list(results), attempts=max(1, attempts))


def cases_from_langfuse(client: Any, name: str, *, limit: int = 50, page_size: int = 50) -> list[EvalCase]:
    """Recent traces named ``name`` as cases: the trace's input is what the
    runner recorded, the call's inputs. No expectations are attached; add
    them, or a judge, to make the cases mean something."""
    cases: list[EvalCase] = []
    page = 1
    while len(cases) < limit:
        response = client.fetch_traces(name=name, page=page, limit=min(page_size, limit - len(cases)))
        data = getattr(response, "data", None) or []
        for trace in data:
            inputs = getattr(trace, "input", None)
            if not isinstance(inputs, dict):
                continue
            cases.append(EvalCase(id=str(getattr(trace, "id", len(cases))), inputs=inputs, source=f"langfuse:{getattr(trace, 'id', '')}"))
        if len(data) < page_size:
            break
        page += 1
    return cases[:limit]


__all__ = [
    "CaseResult",
    "Check",
    "EvalCase",
    "EvalReport",
    "Judge",
    "JudgeVerdict",
    "cases_from_langfuse",
    "expected",
    "run_cases",
]
