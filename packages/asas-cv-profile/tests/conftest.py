"""Standalone fixtures. No network, no model, no real CV: made-up documents and a
made-up profile model (fixtures/profile.yaml), and a scripted stand-in for the model."""

from __future__ import annotations

import asyncio
import io
from collections.abc import Callable
from pathlib import Path

import pytest

from asas_cv_profile import CallResult, Lines, ProfileModel, load_model

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def run(coro):
    """Drive a coroutine from a sync test without an async plugin."""
    return asyncio.run(coro)


@pytest.fixture(scope="session")
def model() -> ProfileModel:
    return load_model(FIXTURES / "profile.yaml")


def lines(*text: str) -> Lines:
    return Lines(list(text), "text")


def docx(*paragraphs: str) -> bytes:
    import docx as python_docx

    d = python_docx.Document()
    for p in paragraphs:
        d.add_paragraph(p)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def blank(section) -> dict:
    """An entry of ``section`` with every field empty, as a strict answer has it."""
    return {x.slug: ([] if x.type in ("text", "multi_enum") else None) for x in section.asked}


class Scripted:
    """The model's stand-in: ``answer(group, section)`` gives a section's entries (a list,
    a dict for a single section, or None for none); every call is recorded."""

    def __init__(self, model: ProfileModel, answer: Callable | None = None, *, fixes: Callable | None = None,
                 kinds: Callable | None = None):
        self.answer, self.fixes, self.kinds = answer, fixes, kinds
        self.sections = {s.slug: s for s in model.sections}
        self.sent: list[tuple[str, str]] = []
        self.release = {"model": "scripted", "id": "0" * 64}

    async def __call__(self, name, text, output_type, what):
        self.sent.append((name, text))
        if name == "LineFix":
            return CallResult(name, output_type.model_validate({"fixes": self.fixes(text)}) if self.fixes else None)
        if name == "OtherKinds":
            return CallResult(name, output_type.model_validate({"blocks": self.kinds(text)}) if self.kinds else None)
        group = self.group_of(output_type)
        data = {}
        for s in group:
            got = self.answer(name, s) if self.answer else None
            data[s.slug] = got if got is not None else ([] if s.repeating else None)
        return CallResult(name, output_type.model_validate(data))

    def group_of(self, output_type) -> list:
        return [self.sections[k] for k in output_type.model_fields]
