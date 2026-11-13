"""A2A client: every malformed reply is an error, never text."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.a2a_client import (
    AgentCallError,
    context_id_for,
    send_message,
    wait_until_reachable,
)

URL = "http://assistant-preview.team-a.svc.cluster.local:8083/"


def call():
    with httpx.Client() as client:
        return send_message(client, URL, "hello", context_id_for("oap-harness-0123456789ab", "one"), 5)


def reply(payload, status=200):
    respx.post(URL).mock(return_value=httpx.Response(status, json=payload))


@respx.mock
def test_completed_task_with_artifacts_returns_text():
    reply(
        {
            "jsonrpc": "2.0",
            "id": "x",
            "result": {
                "kind": "task",
                "status": {"state": "completed"},
                "artifacts": [{"parts": [{"kind": "text", "text": "Hi "}, {"kind": "text", "text": "there"}]}],
            },
        }
    )
    assert call().text == "Hi there"


@respx.mock
def test_task_without_artifacts_falls_back_to_history():
    reply(
        {
            "jsonrpc": "2.0",
            "id": "x",
            "result": {
                "kind": "task",
                "status": {"state": "completed"},
                "history": [
                    {"role": "user", "parts": [{"kind": "text", "text": "q"}]},
                    {"role": "agent", "parts": [{"kind": "text", "text": "answer"}]},
                ],
            },
        }
    )
    assert call().text == "answer"


@respx.mock
def test_message_result_returns_text():
    reply(
        {
            "jsonrpc": "2.0",
            "id": "x",
            "result": {"kind": "message", "role": "agent", "parts": [{"kind": "text", "text": "ok"}]},
        }
    )
    assert call().text == "ok"


@pytest.mark.parametrize(
    "payload,kind",
    [
        ({"jsonrpc": "2.0", "id": "x", "error": {"code": -32603, "message": "boom"}}, "jsonrpc-error"),
        ({"error": "model unavailable"}, "error-payload"),
        # An error next to a result is still an error, with or without jsonrpc 2.0.
        (
            {"jsonrpc": "2.0", "id": "x", "error": "boom", "result": {"kind": "message", "parts": [{"kind": "text", "text": "ok"}]}},
            "error-payload",
        ),
        (
            {"error": {"code": 1}, "result": {"kind": "message", "parts": [{"kind": "text", "text": "ok"}]}},
            "error-payload",
        ),
        (
            {"jsonrpc": "2.0", "id": "x", "result": {"kind": "task", "status": {"state": "failed"}}},
            "task-not-completed",
        ),
        (
            {"jsonrpc": "2.0", "id": "x", "result": {"kind": "message", "parts": [{"kind": "text", "text": "  "}]}},
            "no-text",
        ),
    ],
)
@respx.mock
def test_bad_replies_are_errors(payload, kind):
    reply(payload)
    with pytest.raises(AgentCallError) as exc:
        call()
    assert exc.value.kind == kind
    # The agent produced this reply: the runner scores it as a failed case.
    assert exc.value.agent_side is True


@respx.mock
def test_http_500_is_an_error():
    reply({"x": 1}, status=500)
    with pytest.raises(AgentCallError) as exc:
        call()
    assert exc.value.kind == "http"
    assert exc.value.agent_side is True


@respx.mock
@pytest.mark.parametrize("status", [400, 403, 404, 429])
def test_http_4xx_is_not_agent_side(status):
    # A 4xx comes from the preview path (route, auth, policy, rate limit), not
    # from the revision's answer.
    reply({"x": 1}, status=status)
    with pytest.raises(AgentCallError) as exc:
        call()
    assert exc.value.kind == "http"
    assert exc.value.agent_side is False


@respx.mock
def test_read_timeout_is_agent_side():
    respx.post(URL).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(AgentCallError) as exc:
        call()
    assert exc.value.kind == "timeout"
    assert exc.value.agent_side is True


@respx.mock
def test_connect_timeout_is_not_agent_side():
    respx.post(URL).mock(side_effect=httpx.ConnectTimeout("no route"))
    with pytest.raises(AgentCallError) as exc:
        call()
    assert exc.value.kind == "transport"
    assert exc.value.agent_side is False


@respx.mock
def test_non_json_is_an_error():
    respx.post(URL).mock(return_value=httpx.Response(200, text="<html>"))
    with pytest.raises(AgentCallError) as exc:
        call()
    assert exc.value.kind == "not-json"


@respx.mock
def test_transport_error():
    respx.post(URL).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(AgentCallError) as exc:
        call()
    assert exc.value.kind == "transport"
    assert exc.value.agent_side is False


@respx.mock
def test_request_is_message_send_with_the_harness_context():
    route = respx.post(URL).mock(
        return_value=httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": "x", "result": {"kind": "message", "parts": [{"kind": "text", "text": "ok"}]}},
        )
    )
    call()
    body = json.loads(route.calls.last.request.content)
    assert body["method"] == "message/send"
    message = body["params"]["message"]
    assert message["contextId"].startswith("oap-harness-")
    assert message["contextId"] == "oap-harness-0123456789ab-one"
    assert message["parts"] == [{"kind": "text", "text": "hello"}]
    assert "X-Forwarded-User" not in route.calls.last.request.headers


def test_context_ids_come_from_the_job_name():
    # Review C8: ids from the revision alone collided across datasets on one
    # revision and across agents sharing a pod hash. The Job name hashes
    # namespace/agent/revision/dataset/run, and keeps the excluded marker.
    from app.cluster_loop import HARNESS_SESSION_MARKER
    from app.naming import harness_job_name

    smoke = harness_job_name("team-a", "assistant", "abc123", "example-smoke-v1")
    general = harness_job_name("team-a", "assistant", "abc123", "general-assistant-v1")
    rerun = harness_job_name("team-a", "assistant", "abc123", "example-smoke-v1", run=2)
    other_agent = harness_job_name("team-a", "other", "abc123", "example-smoke-v1")
    ids = [context_id_for(job, "one") for job in (smoke, general, rerun, other_agent)]
    assert ids[0] == f"{smoke}-one"
    assert len(set(ids)) == 4
    assert all(i.startswith(HARNESS_SESSION_MARKER) for i in ids)


def test_context_id_refuses_a_name_without_the_marker():
    with pytest.raises(ValueError):
        context_id_for("assistant-abc123", "one")


@respx.mock
def test_wait_until_reachable_falls_back_to_the_legacy_card():
    respx.get(URL + ".well-known/agent-card.json").mock(return_value=httpx.Response(404))
    respx.get(URL + ".well-known/agent.json").mock(return_value=httpx.Response(200, json={}))
    with httpx.Client() as client:
        assert wait_until_reachable(client, URL, timeout=10, sleep=lambda _s: None)


@respx.mock
def test_wait_until_reachable_gives_up_at_the_deadline():
    respx.get(url__startswith=URL).mock(side_effect=httpx.ConnectError("refused"))
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds

    with httpx.Client() as client:
        assert not wait_until_reachable(
            client, URL, timeout=12, interval=5, clock=lambda: now[0], sleep=sleep
        )
    assert now[0] >= 12
