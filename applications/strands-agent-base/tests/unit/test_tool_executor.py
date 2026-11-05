"""Bounded tool executor: parallelism cap and fleet-question call budget.

A scripted model drives a real strands Agent, so the executor path is the real
one; only the model and the tools are fake.
"""

import asyncio

import pytest
from strands import Agent, tool
from strands.models.model import Model

from app import agent as agent_mod
from app.config import config
from app.tool_executor import BoundedToolExecutor


class ScriptedModel(Model):
    """Turn 1 emits the given tool uses; turn 2 answers with text."""

    def __init__(self, tool_uses):
        self._tool_uses = tool_uses
        self._turn = 0

    def update_config(self, **kw): ...
    def get_config(self): return {}
    async def structured_output(self, *a, **k):
        raise NotImplementedError
        yield

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kw):
        self._turn += 1
        yield {"messageStart": {"role": "assistant"}}
        if self._turn == 1:
            for i, (name, inp) in enumerate(self._tool_uses):
                yield {"contentBlockStart": {"start": {"toolUse": {"toolUseId": f"t{i}", "name": name}}}}
                yield {"contentBlockDelta": {"delta": {"toolUse": {"input": inp}}}}
                yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
        else:
            yield {"contentBlockStart": {"start": {}}}
            yield {"contentBlockDelta": {"delta": {"text": "done"}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "end_turn"}}


def _run(agent, prompt="q"):
    return asyncio.run(agent.invoke_async(prompt))


@pytest.mark.parametrize("limit", [1, 2, 4])
def test_parallelism_never_exceeds_cap(limit):
    state = {"now": 0, "peak": 0, "calls": 0}

    @tool
    async def read_app(name: str) -> str:
        """Read one app."""
        state["now"] += 1
        state["calls"] += 1
        state["peak"] = max(state["peak"], state["now"])
        await asyncio.sleep(0.02)
        state["now"] -= 1
        return name

    uses = [("read_app", f'{{"name": "app{i}"}}') for i in range(12)]
    agent = Agent(
        model=ScriptedModel(uses), tools=[read_app],
        tool_executor=BoundedToolExecutor(limit), callback_handler=None,
    )
    _run(agent)
    assert state["calls"] == 12
    assert state["peak"] <= limit
    if limit > 1:
        assert state["peak"] == limit  # still concurrent, not accidentally serial


def test_limit_must_be_positive():
    with pytest.raises(ValueError):
        BoundedToolExecutor(0)


def test_fleet_question_resolves_unhealthy_app_in_two_calls_or_fewer():
    """With a one-call fleet status tool available, the unhealthy app is found
    in a single tool call (budget: <= 2). The model is scripted to follow the
    prompt guidance; this proves the tool path, not model behaviour (see live plan)."""
    calls = []
    apps = [{"name": f"app{i}", "health": "Healthy"} for i in range(180)]
    apps[77] = {"name": "rust-dev-cd", "health": "Progressing"}

    @tool
    def argocd_unhealthy_applications() -> dict:
        """Fleet-wide Argo CD health in one call."""
        calls.append("fleet")
        bad = [a for a in apps if a["health"] != "Healthy"]
        return {"total": len(apps), "unhealthy": len(bad), "apps": bad}

    agent = Agent(
        model=ScriptedModel([("argocd_unhealthy_applications", "{}")]),
        tools=[argocd_unhealthy_applications],
        tool_executor=BoundedToolExecutor(config.TOOL_MAX_CONCURRENCY),
        callback_handler=None,
    )
    _run(agent, "what is unhealthy?")
    assert len(calls) <= 2
    tool_result = str(agent.messages)
    assert "rust-dev-cd" in tool_result


def test_agent_is_built_with_bounded_executor(monkeypatch):
    seen = {}

    class FakeAgent:
        def __init__(self, **kw):
            seen.update(kw)

    monkeypatch.setattr(agent_mod, "Agent", FakeAgent)
    monkeypatch.setattr(agent_mod, "_build_session_manager", lambda *a: None)
    monkeypatch.setattr(agent_mod, "outbound", lambda *_: ({}, "k"))
    monkeypatch.setattr(agent_mod, "_get_mcp_pool", lambda *a: None)
    monkeypatch.setattr(agent_mod, "_get_model", lambda: object())
    monkeypatch.setattr(agent_mod, "_install_truncation_guard", lambda a: None)
    monkeypatch.setattr(config, "TOOL_MAX_CONCURRENCY", 3)
    agent_mod._construct_agent("s", "a")
    ex = seen["tool_executor"]
    assert isinstance(ex, BoundedToolExecutor) and ex.limit == 3
