"""Long-term memory wiring: per-caller actor, and retrieval from the memory's strategies.

The actor comes from the gateway-set caller identity header, never from parsing a
token. Long-term namespaces come from the memory resource itself (GetMemory), so a
memory without strategies behaves exactly as short-term memory did before.
"""

import asyncio
import re

import pytest

from app import agent as agent_mod
from app import identity
from app.config import config

# The CreateEvent actorId pattern from the botocore API model.
ACTOR_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9-_/]*(?::[a-zA-Z0-9-_/]+)*[a-zA-Z0-9-_/]*$")


# ── caller identity header ───────────────────────────────────────────────

class _Req:
    def __init__(self, headers):
        self.headers = headers


def _through_middleware(headers):
    """Run capture_caller_auth and return caller_subject as seen inside the request."""
    seen = {}

    async def call_next(_request):
        seen["actor"] = identity.caller_subject()
        return "ok"

    asyncio.run(identity.capture_caller_auth(_Req(headers), call_next))
    return seen["actor"]


def test_header_is_the_actor():
    assert _through_middleware({"x-forwarded-user": "3f18797a-3f7e-40ea-8335-0fcab88e7466"}) == (
        "3f18797a-3f7e-40ea-8335-0fcab88e7466"
    )


def test_missing_or_blank_header_means_no_caller():
    assert _through_middleware({}) is None
    assert _through_middleware({"x-forwarded-user": "   "}) is None


def test_authorization_is_not_used_as_identity():
    # A token alone must not become the actor; only the gateway-set header does.
    assert _through_middleware({"authorization": "Bearer abc.def.ghi"}) is None


# ── actor id mapping ─────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("3f18797a-3f7e-40ea-8335-0fcab88e7466", "3f18797a-3f7e-40ea-8335-0fcab88e7466"),
        ("system:serviceaccount:default:oap-assistant-a", "system:serviceaccount:default:oap-assistant-a"),
    ],
)
def test_valid_subjects_pass_through_unchanged(raw, expected):
    assert agent_mod.memory_actor(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["user@example.com", "_leading", ":colon:", "a::b", "space in it", "ünïcode", "x" * 300],
)
def test_other_subjects_are_mapped_onto_the_api_pattern(raw):
    actor = agent_mod.memory_actor(raw)
    assert ACTOR_PATTERN.match(actor), actor
    assert len(actor) <= 255


@pytest.mark.parametrize(
    "a, b",
    [("alice@x.com", "alice_x.com"), ("_x", "a_x"), ("a::b", "a:b"), ("x" * 300, "x" * 301)],
)
def test_mapping_is_one_to_one(a, b):
    # Distinct subjects must never share an actor, or they would share memories.
    assert agent_mod.memory_actor(a) != agent_mod.memory_actor(b)


def test_subject_with_slash_cannot_prefix_match_another_actor():
    # Retrieval matches namespaces by prefix: /facts/a/ would also match /facts/a/b/.
    assert "/" not in agent_mod.memory_actor("a/b")
    assert agent_mod.memory_actor("a/b") != agent_mod.memory_actor("a")


@pytest.mark.parametrize("case", [str.lower, str.upper])
def test_a_subject_cannot_claim_a_hashed_actor_in_any_case(case):
    hashed = agent_mod.memory_actor("user@example.com")
    claimed = "h:" + hashed[2:]
    claimed = case(claimed[:2]) + claimed[2:]
    assert agent_mod.memory_actor(claimed) != hashed


def test_mapping_is_stable():
    assert agent_mod.memory_actor("user@example.com") == agent_mod.memory_actor("user@example.com")


# ── strategy discovery and retrieval config ─────────────────────────────

class _FakeClient:
    strategies: list = []
    calls = 0

    def __init__(self, region_name=None):
        self.region = region_name

    def get_memory_strategies(self, memory_id):
        type(self).calls += 1
        if isinstance(self.strategies, Exception):
            raise self.strategies
        return self.strategies


@pytest.fixture
def fake_client(monkeypatch):
    import bedrock_agentcore.memory as mem

    _FakeClient.strategies, _FakeClient.calls = [], 0
    monkeypatch.setattr(mem, "MemoryClient", _FakeClient)
    monkeypatch.setattr(agent_mod, "_namespaces", {})
    return _FakeClient


def test_no_strategies_means_no_long_term_retrieval(fake_client):
    assert agent_mod._retrieval_config("mem-1", "us-west-2", {}) is None


def test_each_strategy_namespace_gets_a_retrieval_entry(fake_client):
    fake_client.strategies = [
        {"strategyId": "sem-1", "namespaces": ["/facts/{actorId}/"]},
        {"strategyId": "pref-1", "namespaces": ["/preferences/{actorId}/"]},
        {"strategyId": "sum-1", "namespaces": ["/summaries/{actorId}/{sessionId}/"]},
    ]
    rc = agent_mod._retrieval_config("mem-1", "us-west-2", {})
    assert set(rc) == {"/facts/{actorId}/", "/preferences/{actorId}/", "/summaries/{actorId}/{sessionId}/"}
    assert rc["/facts/{actorId}/"].strategy_id == "sem-1"
    assert rc["/facts/{actorId}/"].top_k == 10 and rc["/facts/{actorId}/"].relevance_score == 0.2


def test_retrieval_tuning_comes_from_memory_config(fake_client):
    fake_client.strategies = [{"strategyId": "sem-1", "namespaces": ["/facts/{actorId}/"]}]
    rc = agent_mod._retrieval_config("mem-1", "us-west-2", {"retrievalTopK": "3", "retrievalRelevanceScore": "0.5"})
    assert rc["/facts/{actorId}/"].top_k == 3 and rc["/facts/{actorId}/"].relevance_score == 0.5


def test_strategies_are_cached_within_the_ttl(fake_client):
    fake_client.strategies = [{"strategyId": "sem-1", "namespaces": ["/facts/{actorId}/"]}]
    agent_mod._retrieval_config("mem-1", "us-west-2", {})
    agent_mod._retrieval_config("mem-1", "us-west-2", {})
    assert fake_client.calls == 1


def test_unreadable_strategies_degrade_to_short_term_and_back_off(fake_client, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(agent_mod.time, "monotonic", lambda: now[0])
    fake_client.strategies = RuntimeError("AccessDenied")
    assert agent_mod._retrieval_config("mem-1", "us-west-2", {}) is None
    calls = fake_client.calls
    fake_client.strategies = [{"strategyId": "sem-1", "namespaces": ["/facts/{actorId}/"]}]
    assert agent_mod._retrieval_config("mem-1", "us-west-2", {}) is None  # within the back-off
    assert fake_client.calls == calls
    now[0] += agent_mod._NAMESPACES_RETRY + 1
    assert agent_mod._retrieval_config("mem-1", "us-west-2", {}) is not None


def test_last_good_namespaces_expire_after_the_staleness_bound(fake_client, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(agent_mod.time, "monotonic", lambda: now[0])
    fake_client.strategies = [{"strategyId": "sem-1", "namespaces": ["/facts/{actorId}/"]}]
    agent_mod._retrieval_config("mem-1", "us-west-2", {})
    fake_client.strategies = RuntimeError("throttled")
    now[0] += agent_mod._NAMESPACES_MAX_STALE + 1
    assert agent_mod._retrieval_config("mem-1", "us-west-2", {}) is None


@pytest.mark.parametrize("cfg", [{"retrievalTopK": "ten"}, {"retrievalRelevanceScore": "0,5"}, {"retrievalTopK": ""}])
def test_a_typo_in_retrieval_settings_falls_back_to_defaults(fake_client, cfg):
    fake_client.strategies = [{"strategyId": "sem-1", "namespaces": ["/facts/{actorId}/"]}]
    rc = agent_mod._retrieval_config("mem-1", "us-west-2", cfg)
    assert rc["/facts/{actorId}/"].top_k == 10 and rc["/facts/{actorId}/"].relevance_score == 0.2


def test_summary_namespace_resolves_both_placeholders(fake_client):
    # The SDK fills each namespace with namespace.format(actorId=, sessionId=, ...)
    # (bedrock_agentcore session_manager.retrieve_customer_context); summary's
    # template is the one with two placeholders.
    fake_client.strategies = [{"strategyId": "sum-1", "namespaces": ["/summaries/{actorId}/{sessionId}/"]}]
    (ns,) = agent_mod._retrieval_config("mem-1", "us-west-2", {})
    assert ns.format(actorId="u1", sessionId="s1", memoryStrategyId="sum-1") == "/summaries/u1/s1/"


# ── session manager construction ─────────────────────────────────────────

@pytest.fixture
def agentcore(monkeypatch, fake_client):
    monkeypatch.setattr(config, "MEMORY_PROVIDER", "agentcore")
    monkeypatch.setattr(config, "_MEMORY_CONFIG_RAW", '{"memoryId": "mem-1"}')
    captured = {}

    import bedrock_agentcore.memory.integrations.strands.session_manager as smm

    class _SM:
        def __init__(self, agentcore_memory_config, region_name=None):
            captured["config"], captured["region"] = agentcore_memory_config, region_name

    monkeypatch.setattr(smm, "AgentCoreMemorySessionManager", _SM)
    return captured


def test_session_manager_uses_the_mapped_actor_and_agent_region(agentcore, monkeypatch):
    monkeypatch.setattr(config, "AWS_REGION", "eu-west-1")
    agent_mod._build_session_manager("ctx-1", "user@example.com")
    cfg = agentcore["config"]
    assert cfg.actor_id == agent_mod.memory_actor("user@example.com")
    assert cfg.actor_id.startswith("h:")
    assert cfg.session_id == "ctx-1"
    assert agentcore["region"] == "eu-west-1"  # no region in MEMORY_CONFIG -> the agent's own
    assert cfg.retrieval_config is None


def test_session_manager_turns_on_long_term_when_the_memory_has_strategies(agentcore, fake_client):
    fake_client.strategies = [{"strategyId": "sem-1", "namespaces": ["/facts/{actorId}/"]}]
    agent_mod._build_session_manager("ctx-1", "user-1")
    assert set(agentcore["config"].retrieval_config) == {"/facts/{actorId}/"}


def test_create_agent_takes_the_actor_from_the_request(monkeypatch):
    seen = {}
    monkeypatch.setattr(agent_mod, "_construct_agent", lambda sid, actor: seen.setdefault("actor", actor))
    token = identity.inbound_actor.set("user-7")
    try:
        agent_mod.create_agent("ctx-9")
    finally:
        identity.inbound_actor.reset(token)
    assert seen["actor"] == "user-7"


def test_create_agent_without_header_has_no_actor(monkeypatch):
    seen = {}
    monkeypatch.setattr(agent_mod, "_construct_agent", lambda sid, actor: seen.setdefault("actor", actor))
    agent_mod.create_agent("ctx-9")
    assert seen["actor"] is None


def test_no_caller_means_no_memory(agentcore, fake_client):
    # Fail closed: requests without the gateway header must not share an actor.
    fake_client.strategies = [{"strategyId": "sem-1", "namespaces": ["/facts/{actorId}/"]}]
    assert agent_mod._build_session_manager("ctx-1", None) is None
    assert "config" not in agentcore


def test_strategies_added_after_startup_are_picked_up_after_the_ttl(fake_client, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(agent_mod.time, "monotonic", lambda: now[0])
    fake_client.strategies = [{"strategyId": "sem-1", "namespaces": ["/facts/{actorId}/"]}]
    assert set(agent_mod._retrieval_config("mem-1", "us-west-2", {})) == {"/facts/{actorId}/"}

    fake_client.strategies.append({"strategyId": "pref-1", "namespaces": ["/preferences/{actorId}/"]})
    now[0] += agent_mod._NAMESPACES_TTL - 1
    assert set(agent_mod._retrieval_config("mem-1", "us-west-2", {})) == {"/facts/{actorId}/"}
    now[0] += 2
    assert set(agent_mod._retrieval_config("mem-1", "us-west-2", {})) == {"/facts/{actorId}/", "/preferences/{actorId}/"}


def test_a_failed_refresh_keeps_the_last_good_namespaces(fake_client, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(agent_mod.time, "monotonic", lambda: now[0])
    fake_client.strategies = [{"strategyId": "sem-1", "namespaces": ["/facts/{actorId}/"]}]
    agent_mod._retrieval_config("mem-1", "us-west-2", {})
    fake_client.strategies = RuntimeError("throttled")
    now[0] += agent_mod._NAMESPACES_TTL + 1
    assert set(agent_mod._retrieval_config("mem-1", "us-west-2", {})) == {"/facts/{actorId}/"}
