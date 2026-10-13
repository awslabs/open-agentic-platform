"""Session isolation must hold regardless of which credential goes outbound.

With PROPAGATE_CALLER_TOKEN=false the agent presents its own workload token to
MCP servers, so the OUTBOUND key is the same constant for every caller. The
session cache must still key on the INBOUND caller, or two users who send the
same contextId would share one agent and its conversation history.
"""

from collections import OrderedDict

import pytest

from app import agent as agent_mod
from app.config import config
from app.identity import inbound_actor, inbound_auth, request_id


@pytest.fixture
def fake_create(monkeypatch):
    created = []

    def fake_create_agent(session_id=None, actor_id="user"):
        a = object()
        created.append(a)
        return a

    monkeypatch.setattr(agent_mod, "create_agent", fake_create_agent)
    monkeypatch.setattr(agent_mod, "_agents", OrderedDict())
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


def _as(sub, auth, req="r1"):
    """Run get_or_create_agent as a request with this subject, token and request id."""
    tokens = [inbound_actor.set(sub), inbound_auth.set(auth), request_id.set(req)]
    try:
        return agent_mod.get_or_create_agent(session_id="ctx")[0]
    finally:
        for var, t in zip((inbound_actor, inbound_auth, request_id), tokens):
            var.reset(t)


def test_without_forwarding_a_token_refresh_keeps_the_agent(monkeypatch, fake_create):
    monkeypatch.setattr(config, "PROPAGATE_CALLER_TOKEN", False)
    assert _as("alice", "Bearer old") is _as("alice", "Bearer refreshed")


def test_when_forwarding_a_token_refresh_gets_a_new_agent(monkeypatch, fake_create):
    # The cached agent's MCP connections carry the old token.
    monkeypatch.setattr(config, "PROPAGATE_CALLER_TOKEN", True)
    assert _as("alice", "Bearer old") is not _as("alice", "Bearer refreshed")


@pytest.mark.parametrize("propagate", [True, False])
def test_subjects_are_isolated_even_with_no_passthrough_token(monkeypatch, fake_create, propagate):
    # credentialPassthrough disabled at the gateway: no bearer reaches the agent.
    monkeypatch.setattr(config, "PROPAGATE_CALLER_TOKEN", propagate)
    assert _as("alice", None) is not _as("bob", None)
    assert _as("alice", None) is _as("alice", None)


def test_requests_with_no_identity_never_share_an_agent(monkeypatch, fake_create):
    monkeypatch.setattr(config, "PROPAGATE_CALLER_TOKEN", False)
    assert _as(None, None, req="r1") is not _as(None, None, req="r2")


def test_cache_is_bounded(monkeypatch, fake_create):
    monkeypatch.setattr(config, "PROPAGATE_CALLER_TOKEN", False)
    monkeypatch.setattr(agent_mod, "_MAX_AGENTS", 2)
    first = _as("a", None)
    _as("b", None)
    _as("a", None)  # touch: a is now most recent
    _as("c", None)  # evicts b
    assert len(agent_mod._agents) == 2
    assert _as("a", None) is first
