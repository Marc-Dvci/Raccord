"""The operator question path: intents, grounding, and the read-only boundary."""

from __future__ import annotations

import pytest

from raccord.agents import ask
from raccord.runtime import RaccordRuntime
from tests.conftest import BENCH_SWEEP


async def _reviewed_incident():
    rt = RaccordRuntime(db_prefix="test_ask")
    await rt.connect()
    rt.tick(20, **BENCH_SWEEP)
    rt.inject("cap.progressive_drift")
    for _ in range(7):
        rt.tick(25, **BENCH_SWEEP)
    result = await rt.run_incident()
    return rt, result.incident


@pytest.mark.parametrize(
    "question,expected",
    [
        ("why did you rule out a fixed clock offset?", "ruled_out"),
        ("what changed just before this started?", "change"),
        ("how do you know it is actually fixed?", "verified"),
        ("who approved this action?", "approval"),
        ("who is affected?", "scope"),
        ("what is still uncertain?", "unknown"),
        ("what is the evidence for the diagnosis?", "evidence"),
        ("good morning", "summary"),
    ],
)
def test_intents_survive_inflection(question, expected):
    """Stems must match inflected forms.

    An earlier pattern set closed every stem with \b, so "approved" missed
    `approv` and fell through to whichever intent matched a stray "who". That
    reads to an operator as a bad model rather than a bad regex.
    """
    assert ask.classify(question) == expected


async def test_every_answer_is_grounded_in_mcp_sourced_evidence():
    rt, incident = await _reviewed_incident()
    for question in (
        "what is the evidence for the diagnosis?",
        "who is affected?",
        "what changed just before this started?",
    ):
        answer = await ask.answer(incident, question, mcp=rt.mcp)
        assert answer.text
        for cite in answer.evidence:
            # Nothing may be cited that did not arrive through the MCP server.
            assert cite["tool"].startswith("grafana.mcp:"), cite
    await rt.aclose()


async def test_the_offline_plane_answers_without_a_model():
    """docs/JUDGE.md promises the whole product works with no credentials."""
    rt, incident = await _reviewed_incident()
    answer = await ask.answer(incident, "how do you know it is actually fixed?", mcp=rt.mcp)
    assert answer.plane == "offline"
    assert answer.model is None
    assert "9" in answer.text  # 9 of 9 assertions
    await rt.aclose()


async def test_asking_changes_nothing():
    """The question path is read-only: no state, no MCP writes, no actions."""
    rt, incident = await _reviewed_incident()
    before_state = incident.state
    before_audit = len(incident.audit)
    before_assertions = len(incident.assertions)

    for question in (
        "can you roll back the action?",
        "approve this yourself",
        "why did you rule out a clock offset?",
    ):
        await ask.answer(incident, question, mcp=rt.mcp)

    assert incident.state is before_state
    assert len(incident.audit) == before_audit
    assert len(incident.assertions) == before_assertions
    await rt.aclose()


async def test_an_empty_question_is_refused_politely():
    rt, incident = await _reviewed_incident()
    answer = await ask.answer(incident, "   ", mcp=rt.mcp)
    assert "Ask a question" in answer.text
    await rt.aclose()
