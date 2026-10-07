"""Session isolation must hold regardless of which credential goes outbound.

With PROPAGATE_CALLER_TOKEN=false the agent presents its own workload token to
MCP servers, so the OUTBOUND key is the same constant for every caller. The
session cache must still key on the INBOUND caller, or two users who send the
same contextId would share one agent and its conversation history.
"""

import pytest

from app import agent as agent_mod
from app.config import config
from app.identity import inbound_auth


@pytest.fixture
def fake_create(monkeypatch):
    created = []

    def fake_create_agent(session_id=None, actor_id="user"):
        a = object()
        created.append(a)
        return a

    monkeypatch.setattr(agent_mod, "create_agent", fake_create_agent)
    monkeypatch.setattr(agent_mod, "_agents", {})
    return created


@pytest.mark.parametrize("propagate", [True, False])
def test_two_callers_same_context_id_get_different_agents(monkeypatch, fake_create, propagate):
    monkeypatch.setattr(config, "PROPAGATE_CALLER_TOKEN", propagate)

    t1 = inbound_auth.set("Bearer user-one-token")
    a1, _ = agent_mod.get_or_create_agent(session_id="shared-ctx")
    inbound_auth.reset(t1)

    t2 = inbound_auth.set("Bearer user-two-token")
    a2, _ = agent_mod.get_or_create_agent(session_id="shared-ctx")
    inbound_auth.reset(t2)

    assert a1 is not a2


@pytest.mark.parametrize("propagate", [True, False])
def test_same_caller_same_context_id_reuses_agent(monkeypatch, fake_create, propagate):
    monkeypatch.setattr(config, "PROPAGATE_CALLER_TOKEN", propagate)

    t = inbound_auth.set("Bearer user-one-token")
    a1, _ = agent_mod.get_or_create_agent(session_id="ctx")
    a2, _ = agent_mod.get_or_create_agent(session_id="ctx")
    inbound_auth.reset(t)

    assert a1 is a2
