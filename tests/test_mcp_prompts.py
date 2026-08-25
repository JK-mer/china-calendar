"""MCP stored runbooks (#41): registered, and carrying the I1 guardrails."""

import asyncio

import pytest


@pytest.fixture(scope="module")
def mcp(tmp_path_factory):
    import os
    os.environ.setdefault("PC_STORE_DIR", str(tmp_path_factory.mktemp("store")))
    from china_calendar.mcp_server import mcp as server
    return server


def test_prompts_registered(mcp):
    prompts = asyncio.run(mcp.list_prompts())
    names = {p.name for p in prompts}
    assert {"verify_unverified", "quarterly_outlook", "research_and_add",
            "check_missing"} <= names


def _text(mcp, name, args=None):
    result = asyncio.run(mcp.render_prompt(name, args))
    return " ".join(m.content.text for m in result.messages)


def test_verify_prompt_carries_the_guardrails(mcp):
    text = _text(mcp, "verify_unverified")
    assert "NEVER use human_stated=true" in text
    assert "verbatim" in text and "bot-walled" in text
    assert "RETURNED status" in text


def test_outlook_prompt_parameterises_horizon(mcp):
    # MCP prompt arguments are protocol-level strings; fastmcp coerces to
    # the declared int
    assert "upcoming(days=180)" in _text(mcp, "quarterly_outlook",
                                         {"horizon_days": "180"})
    assert "upcoming(days=90)" in _text(mcp, "quarterly_outlook")


def test_research_prompt_takes_topic_and_window(mcp):
    text = _text(mcp, "research_and_add",
                 {"topic": "EU-China dialogues", "window": "H2 2026"})
    assert "EU-China dialogues" in text and "H2 2026" in text
    assert "human_stated" in text


def test_check_missing_separates_the_three_lists(mcp):
    """The runbook is only useful if it stops the model treating a standing
    watch as a gap — that is how speculative dates get invented (#67)."""
    text = _text(mcp, "check_missing",
                 {"from_date": "2026-08-11", "to_date": "2026-11-09"})
    assert "missing_expected('2026-08-11', '2026-11-09')" in text
    assert "THE RESEARCH QUEUE" in text
    assert "CONTEXT, NEVER A GAP" in text
    assert "REPORT, DO NOT RESEARCH" in text


def test_check_missing_carries_the_provenance_guardrails(mcp):
    text = _text(mcp, "check_missing",
                 {"from_date": "2026-08-11", "to_date": "2026-11-09"})
    assert "NEVER use human_stated=true" in text
    assert "verbatim" in text and "original language" in text
    assert "RETURNED status" in text
