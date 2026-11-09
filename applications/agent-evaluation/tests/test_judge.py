"""The harness judge: an unparseable verdict is an error, never a pass."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.judge import JudgeError, judge, render_prompt

BIFROST = "http://bifrost.bifrost.svc.cluster.local:8080/v1"
ENDPOINT = f"{BIFROST}/chat/completions"


def completion(content: str, status: int = 200):
    return respx.post(ENDPOINT).mock(
        return_value=httpx.Response(
            status, json={"choices": [{"message": {"role": "assistant", "content": content}}]}
        )
    )


def run(key="sk-bf-test"):
    with httpx.Client() as client:
        return judge(client, BIFROST, key, "us.amazon.nova-pro-v1:0", "prompt", 5)


@respx.mock
def test_valid_json_verdict():
    route = completion('{"score": 0.9, "reasoning": "matches"}')
    verdict = run()
    assert verdict.score == 0.9
    assert verdict.reasoning == "matches"
    body = json.loads(route.calls.last.request.content)
    assert body["model"] == "us.amazon.nova-pro-v1:0"
    assert body["temperature"] == 0
    assert body["messages"][0]["role"] == "system"
    assert "untrusted" in body["messages"][0]["content"]


@respx.mock
def test_json_inside_prose():
    completion('Sure. {"score": 1, "reasoning": "ok"} Hope that helps {x}')
    assert run().score == 1.0


@respx.mock
@pytest.mark.parametrize(
    "content", ['{"score": 1.2}', "no json here", '{"score": "high"}', '{"score": true}']
)
def test_bad_verdicts_are_errors(content):
    completion(content)
    with pytest.raises(JudgeError):
        run()


@respx.mock
def test_http_error_is_an_error():
    completion("{}", status=502)
    with pytest.raises(JudgeError):
        run()


@respx.mock
def test_virtual_key_header_only_when_a_key_exists():
    route = completion('{"score": 0.5}')
    run(key="sk-bf-abc")
    headers = route.calls.last.request.headers
    assert headers["x-bf-vk"] == "sk-bf-abc"
    assert headers["authorization"] == "Bearer sk-bf-abc"
    run(key="")
    headers = route.calls.last.request.headers
    assert "x-bf-vk" not in headers
    assert "authorization" not in headers


def test_prompt_tokens_are_substituted_and_output_truncated():
    out = render_prompt("I={{input}} E={{expected}} O={{output}}", "q", "a", "x" * 9000)
    assert out.startswith("I=q E=a O=")
    assert out.count("x") == 8000
