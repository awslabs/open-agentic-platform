"""Unit tests for the graceful max_tokens truncation guard.

When a generation hits the per-request output cap, Strands raises
MaxTokensReachedException *after* streaming the partial answer. The guard
installed by ``_install_truncation_guard`` must convert that unhandled raise
(which otherwise fails the A2A task with an opaque "Agent execution failed")
into a graceful end-of-turn: stream the partial answer, append a visible
notice, and yield a terminal result so the task completes.
"""

import types

import pytest

from app import agent as agent_mod
from app.config import config
from strands.agent.agent_result import AgentResult
from strands.types.exceptions import MaxTokensReachedException


def _fake_agent(stream_impl):
    """Minimal stand-in exposing the two attributes the guard touches."""
    return types.SimpleNamespace(
        stream_async=stream_impl,
        event_loop_metrics=object(),
    )


async def _drain(agen):
    return [event async for event in agen]


@pytest.mark.asyncio
async def test_truncation_is_converted_to_notice_plus_terminal_result(monkeypatch):
    monkeypatch.setattr(config, "MAX_TOKENS_NOTICE_ENABLED", True)
    monkeypatch.setattr(config, "MAX_TOKENS_NOTICE", "TRUNCATED-NOTICE")

    async def stream(*args, **kwargs):
        yield {"data": "partial answer "}
        yield {"data": "that got cut"}
        raise MaxTokensReachedException("max tokens")

    agent = _fake_agent(stream)
    agent_mod._install_truncation_guard(agent)

    events = await _drain(agent.stream_async("hi"))

    # The two real deltas are passed through untouched.
    assert events[0] == {"data": "partial answer "}
    assert events[1] == {"data": "that got cut"}
    # The notice is streamed as the tail of the answer so the UI renders it.
    assert events[2]["data"].strip() == "TRUNCATED-NOTICE"
    # A terminal result is emitted so the A2A executor completes (not fails)
    # the task and invoke_async returns cleanly.
    result = events[3]["result"]
    assert isinstance(result, AgentResult)
    assert result.stop_reason == "max_tokens"
    # str(result) (used by /chat) renders the partial answer + the notice.
    rendered = str(result)
    assert "partial answer that got cut" in rendered
    assert "TRUNCATED-NOTICE" in rendered


@pytest.mark.asyncio
async def test_normal_stream_is_passed_through_unchanged(monkeypatch):
    monkeypatch.setattr(config, "MAX_TOKENS_NOTICE_ENABLED", True)
    sentinel_result = AgentResult(
        stop_reason="end_turn",
        message={"role": "assistant", "content": [{"text": "all done"}]},
        metrics=object(),
        state={},
    )

    async def stream(*args, **kwargs):
        yield {"data": "all done"}
        yield {"result": sentinel_result}

    agent = _fake_agent(stream)
    agent_mod._install_truncation_guard(agent)

    events = await _drain(agent.stream_async("hi"))

    assert events == [{"data": "all done"}, {"result": sentinel_result}]


@pytest.mark.asyncio
async def test_other_exceptions_still_propagate(monkeypatch):
    monkeypatch.setattr(config, "MAX_TOKENS_NOTICE_ENABLED", True)

    async def stream(*args, **kwargs):
        yield {"data": "boom incoming"}
        raise RuntimeError("not a token limit")

    agent = _fake_agent(stream)
    agent_mod._install_truncation_guard(agent)

    with pytest.raises(RuntimeError, match="not a token limit"):
        await _drain(agent.stream_async("hi"))


def test_disabled_toggle_is_a_noop(monkeypatch):
    monkeypatch.setattr(config, "MAX_TOKENS_NOTICE_ENABLED", False)

    async def stream(*args, **kwargs):
        yield {"data": "x"}

    agent = _fake_agent(stream)
    original = agent.stream_async
    agent_mod._install_truncation_guard(agent)

    # Guard disabled → stream_async is left exactly as-is.
    assert agent.stream_async is original
