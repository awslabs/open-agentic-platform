"""Delegation to sub-agents (app/delegation.py, docs/agent-delegation/DESIGN.md)."""

import asyncio

import httpx
import pytest

from app import agent as agent_mod
from app import delegation
from app.config import config
from app.identity import inbound_actor, inbound_depth_raw, request_id

CARD = {
    "name": "math-agent",
    "description": "Solves arithmetic.",
    "skills": [{"id": "calc", "name": "calc", "description": "evaluate expressions"}],
}


class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    token = tmp_path / "token"
    token.write_text("sa-token")
    monkeypatch.setenv("WORKLOAD_TOKEN_PATH", str(token))
    monkeypatch.setenv("A2A_AGENT_NAMES", "math-agent, poet-agent,math-agent")
    monkeypatch.setattr(config, "AGENT_NAME", "supervisor-agent")
    monkeypatch.setattr(delegation, "_cards", {})
    monkeypatch.setattr(delegation, "_counts", delegation.OrderedDict())
    # The first delegation_tools() call fetches synchronously; no background loop here.
    monkeypatch.setattr(delegation, "_refresher_started", False)
    monkeypatch.setattr(delegation, "_refresh_loop", lambda: None)
    tokens = [inbound_actor.set(None), inbound_depth_raw.set(None), request_id.set("req-1")]
    yield
    for var, t in zip((inbound_actor, inbound_depth_raw, request_id), tokens):
        var.reset(t)


def _cards(monkeypatch, by_name):
    """Serve cards per agent name; a value that is an int is an HTTP error status."""
    calls = []

    def fake_get(url, headers=None, timeout=None):
        name = url.split("/")[3]
        calls.append((name, headers))
        v = by_name.get(name, 404)
        return _Resp(v, None) if isinstance(v, int) else _Resp(200, v)

    monkeypatch.setattr(delegation.httpx, "get", fake_get)
    return calls


class _Client:
    """httpx.AsyncClient stand-in recording posts and returning one canned answer."""

    posts: list = []
    answer = None

    def __init__(self, timeout=None):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        _Client.posts.append((url, json, headers))
        if isinstance(_Client.answer, Exception):
            raise _Client.answer
        return _Client.answer


@pytest.fixture
def client(monkeypatch):
    _Client.posts = []
    _Client.answer = _Resp(200, {"result": {"kind": "task", "status": {"state": "completed"},
                                            "artifacts": [{"parts": [{"kind": "text", "text": "391"}]}]}})
    monkeypatch.setattr(delegation.httpx, "AsyncClient", _Client)
    return _Client


def _call(tool, task="what is 17*23?"):
    return asyncio.run(tool._tool_func({"toolUseId": "t1", "input": {"task": task}}))


def test_agent_names_dedupes_and_skips_itself(monkeypatch):
    monkeypatch.setenv("A2A_AGENT_NAMES", "a, b,a,supervisor-agent,,")
    assert delegation.agent_names() == ["a", "b"]


def test_card_url_and_credential_are_the_gateway_route_and_own_token(monkeypatch):
    calls = _cards(monkeypatch, {"math-agent": CARD})
    delegation.refresh(force=True)
    assert ("math-agent", {"Authorization": "Bearer sa-token"}) in calls
    assert delegation.agent_url("math-agent") == f"{config.GATEWAY_URL}/math-agent/"


def test_one_tool_per_available_agent_and_unavailable_ones_are_reported(monkeypatch):
    _cards(monkeypatch, {"math-agent": CARD, "poet-agent": 403})
    tools, available, unavailable = delegation.delegation_tools("s1")
    assert [t.tool_name for t in tools] == ["ask_math-agent"]
    assert (available, unavailable) == (["math-agent"], ["poet-agent"])
    desc = tools[0].tool_spec["description"]
    assert "Solves arithmetic." in desc and "calc: evaluate expressions" in desc
    assert "unavailable: poet-agent" in delegation.prompt_addendum(available, unavailable)


def test_building_tools_reads_the_cache_and_never_fetches(monkeypatch):
    calls = _cards(monkeypatch, {"math-agent": CARD})
    delegation.delegation_tools("s1")  # first call: one synchronous refresh
    n = len(calls)
    for _ in range(3):
        delegation.delegation_tools("s1")
    assert len(calls) == n


def test_refresh_fetches_only_due_cards_and_keeps_the_last_good_card(monkeypatch):
    calls = _cards(monkeypatch, {"math-agent": CARD})
    delegation.refresh(force=True)
    n = len(calls)
    delegation.refresh()  # nothing due yet
    assert len(calls) == n
    monkeypatch.setattr(delegation, "_CARD_TTL", 0.0)
    _cards(monkeypatch, {"math-agent": 503})
    delegation.refresh()
    assert delegation.card("math-agent")[0] == CARD


def test_generation_changes_when_a_sub_agent_appears(monkeypatch):
    _cards(monkeypatch, {"math-agent": CARD, "poet-agent": 404})
    delegation.refresh(force=True)
    before = delegation.generation()
    assert before == ("math-agent",)
    _cards(monkeypatch, {"math-agent": CARD, "poet-agent": CARD})
    delegation.refresh(force=True)
    assert delegation.generation() == ("math-agent", "poet-agent") != before


def test_agent_built_on_an_old_sub_agent_set_is_stale(monkeypatch):
    _cards(monkeypatch, {"math-agent": CARD, "poet-agent": 404})
    delegation.refresh(force=True)
    built = type("A", (), {"_oap_delegation": delegation.generation()})()
    assert not agent_mod.agent_is_stale(built)
    _cards(monkeypatch, {"math-agent": CARD, "poet-agent": CARD})
    delegation.refresh(force=True)
    assert agent_mod.agent_is_stale(built)


def test_missing_gateway_token_is_an_error_at_startup(monkeypatch, caplog):
    monkeypatch.delenv("WORKLOAD_TOKEN_PATH")
    _cards(monkeypatch, {})
    with caplog.at_level("ERROR"):
        delegation.ensure_refresher()
    assert "gateway-identity" in caplog.text


def test_outbound_headers_name_the_user_and_the_next_depth():
    inbound_actor.set("user-1")
    inbound_depth_raw.set("1")
    h = delegation.outbound_headers()
    assert h == {"Authorization": "Bearer sa-token", "x-delegated-user": "user-1", "x-delegation-depth": "2"}


def test_no_user_means_no_delegated_user_header():
    h = delegation.outbound_headers()
    assert "x-delegated-user" not in h and h["x-delegation-depth"] == "1"


def test_call_sends_a2a_message_with_a_stable_context_and_returns_the_text(monkeypatch, client):
    _cards(monkeypatch, {"math-agent": CARD})
    inbound_actor.set("user-1")
    tool = delegation.delegation_tools("sess-9")[0][0]
    out = _call(tool)
    assert out == {"toolUseId": "t1", "status": "success", "content": [{"text": "391"}]}
    url, body, headers = client.posts[0]
    assert url == f"{config.GATEWAY_URL}/math-agent/"
    assert body["method"] == "message/send"
    assert body["params"]["message"]["contextId"] == "sess-9-math-agent"
    assert body["params"]["message"]["parts"] == [{"kind": "text", "text": "what is 17*23?"}]
    assert headers["x-delegated-user"] == "user-1"


@pytest.mark.parametrize(
    "answer,needle",
    [
        (_Resp(403, None), "HTTP 403"),
        (_Resp(200, {"error": {"code": -32001, "message": "nope"}}), "A2A error -32001"),
        (_Resp(200, {"result": {"status": {"state": "failed"}}}), "ended failed"),
        (_Resp(200, ValueError("bad")), "not JSON"),
        (_Resp(200, ["not", "an", "object"]), "not a JSON-RPC object"),
        (_Resp(200, {"result": "x"}), "no result"),
        (_Resp(200, {"result": {"status": {"state": "input-required",
                                           "message": {"parts": [{"kind": "text", "text": "which unit?"}]}}}}),
         "needs input it cannot get through delegation: which unit?"),
        (httpx.ReadTimeout("slow"), "did not answer"),
        (httpx.ConnectError("down"), "could not be reached"),
    ],
)
def test_failures_come_back_as_tool_errors_not_exceptions(monkeypatch, client, answer, needle):
    _cards(monkeypatch, {"math-agent": CARD})
    client.answer = answer
    out = _call(delegation.delegation_tools("s")[0][0])
    assert out["status"] == "error" and needle in out["content"][0]["text"]


def test_depth_limit_refuses_without_calling(monkeypatch, client):
    _cards(monkeypatch, {"math-agent": CARD})
    inbound_depth_raw.set(str(delegation.MAX_DEPTH))
    out = _call(delegation.delegation_tools("s")[0][0])
    assert out["status"] == "error" and "depth limit" in out["content"][0]["text"]
    assert client.posts == []


def test_per_request_cap(monkeypatch, client):
    _cards(monkeypatch, {"math-agent": CARD})
    monkeypatch.setattr(delegation, "_MAX_PER_REQUEST", 2)
    tool = delegation.delegation_tools("s")[0][0]
    assert [_call(tool)["status"] for _ in range(3)] == ["success", "success", "error"]
    request_id.set("req-2")  # a new request starts from zero
    assert _call(tool)["status"] == "success"


def test_constructed_agent_gets_the_tools_and_the_supervisor_prompt(monkeypatch):
    _cards(monkeypatch, {"math-agent": CARD, "poet-agent": 404})
    captured = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(agent_mod, "Agent", FakeAgent)
    monkeypatch.setattr(agent_mod, "_get_model", lambda: None)
    monkeypatch.setattr(agent_mod, "_build_session_manager", lambda *a: None)
    monkeypatch.setattr(agent_mod, "_install_truncation_guard", lambda a: None)
    monkeypatch.setattr(config, "MCP_SERVER_NAMES_RAW", None)
    agent_mod._construct_agent("s1", None)
    assert [t.tool_name for t in captured["tools"]] == ["ask_math-agent"]
    assert captured["system_prompt"].startswith(config.SYSTEM_PROMPT)
    assert "ask_<agent>" in captured["system_prompt"] and "unavailable: poet-agent" in captured["system_prompt"]


def test_agent_without_sub_agents_is_unchanged(monkeypatch):
    monkeypatch.delenv("A2A_AGENT_NAMES")
    captured = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(agent_mod, "Agent", FakeAgent)
    monkeypatch.setattr(agent_mod, "_get_model", lambda: None)
    monkeypatch.setattr(agent_mod, "_build_session_manager", lambda *a: None)
    monkeypatch.setattr(agent_mod, "_install_truncation_guard", lambda a: None)
    monkeypatch.setattr(config, "MCP_SERVER_NAMES_RAW", None)
    agent_mod._construct_agent("s1", None)
    assert captured["tools"] is None and captured["system_prompt"] == config.SYSTEM_PROMPT


def test_status_message_text_is_used_when_there_are_no_artifacts():
    ok, text = delegation._reply_text({"result": {"status": {"state": "completed",
                                                             "message": {"parts": [{"kind": "text", "text": "42"}]}}}})
    assert (ok, text) == (True, "42")
