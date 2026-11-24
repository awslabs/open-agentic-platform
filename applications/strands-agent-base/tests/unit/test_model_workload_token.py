"""The model sends the projected ServiceAccount token when LLM_GATEWAY_AUTH_MODE is set.

Guardrail routes sit behind the agent gateway's JWT policy. The token rotates, so it
is read on every request, not once when the model is built.
"""
import asyncio
import logging

import httpx
import openai
import pytest

from app import agent as agent_mod

BIFROST_ARGS = {"api_key": "vk-1", "base_url": "http://gw/v1", "default_headers": {"x-bf-vk": "vk-1"}}


@pytest.fixture
def token_file(tmp_path, monkeypatch):
    path = tmp_path / "token"
    path.write_text("sa-token-v1")
    monkeypatch.setenv("WORKLOAD_TOKEN_PATH", str(path))
    return path


@pytest.fixture
def model_args(monkeypatch):
    captured = {}
    monkeypatch.setattr(agent_mod, "_model", None)
    monkeypatch.setattr(agent_mod.config, "LLM_GATEWAY_API_KEY", "vk-1")
    monkeypatch.setattr(agent_mod.config, "LLM_GATEWAY_URL", "http://gw/v1")
    monkeypatch.setattr(agent_mod, "OpenAIModel", lambda **kw: captured.update(kw))

    def build(mode):
        monkeypatch.setattr(agent_mod.config, "LLM_GATEWAY_AUTH_MODE", mode)
        monkeypatch.setattr(agent_mod, "_model", None)
        agent_mod._get_model()
        return captured["client_args"]

    return build


def _sent_authorization(client_args) -> str:
    """Build the client the way strands does (a new AsyncOpenAI per request) and send one call."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("authorization")
        seen["vk"] = request.headers.get("x-bf-vk")
        return httpx.Response(200, json={"object": "list", "data": []})

    async def run():
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        async with openai.AsyncOpenAI(**client_args, http_client=http) as client:
            await client.models.list()

    asyncio.run(run())
    return seen


def test_mode_unset_keeps_the_client_args_unchanged(model_args, token_file):
    assert model_args("") == BIFROST_ARGS


def test_mode_set_sends_the_token_as_bearer_and_keeps_the_virtual_key(model_args, token_file):
    seen = _sent_authorization(model_args("workload-token"))
    assert seen == {"authorization": "Bearer sa-token-v1", "vk": "vk-1"}


def test_the_next_request_carries_a_rotated_token_without_rebuilding_the_model(model_args, token_file):
    args = model_args("workload-token")
    assert _sent_authorization(args)["authorization"] == "Bearer sa-token-v1"
    token_file.write_text("sa-token-v2\n")
    assert _sent_authorization(args)["authorization"] == "Bearer sa-token-v2"


def test_a_missing_token_file_does_not_raise_and_the_cause_is_logged(model_args, tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("WORKLOAD_TOKEN_PATH", str(tmp_path / "absent"))
    with caplog.at_level(logging.WARNING):
        seen = _sent_authorization(model_args("workload-token"))
    assert seen["authorization"] == "Bearer not-used"
    assert "unreadable" in caplog.text


def test_the_token_never_appears_in_a_log_record(model_args, token_file, caplog):
    with caplog.at_level(logging.DEBUG, logger="app"):
        _sent_authorization(model_args("workload-token"))
    assert "sa-token-v1" not in caplog.text
