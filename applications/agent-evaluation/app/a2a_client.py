"""A2A `message/send` client for the harness runner.

Every way a reply can be wrong is an AgentCallError, never a reply: a harness
that counts a JSON-RPC error or a failed Task as "some text" would score a
broken revision as passing. `kind` is a short stable word for the result's
reason field and the logs.

`agent_side` says who produced the failure. A reply the agent itself gave
that is not a usable answer (failed Task, JSON-RPC error, error payload,
non-JSON, no text, HTTP 5xx) or an agent that did not answer in time is the
revision's fault: the runner scores it as a FAILED case. A connect or other
transport error and an HTTP 4xx from the preview path (auth, policy, 429) say
nothing about the revision's answers: the runner counts them as case errors,
outside the pass ratio.

No `X-Forwarded-User` is sent, so the platform base image falls back to a
per-request caller with no memory, which keeps cases independent. The contextId
carries the harness marker so online evaluation skips these traces.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

import httpx

from .cluster_loop import HARNESS_SESSION_MARKER

AGENT_CARD_PATHS = (".well-known/agent-card.json", ".well-known/agent.json")


class AgentCallError(Exception):
    def __init__(self, kind: str, detail: str, agent_side: bool = True):
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail
        self.agent_side = agent_side


@dataclass(frozen=True)
class AgentReply:
    text: str
    raw_state: str | None


def context_id_for(job_name: str, case_id: str) -> str:
    """`<job-name>-<caseId>`, i.e. `oap-harness-<12 hex>-<caseId>`.

    The Job name hashes namespace, agent, revision, dataset and run number, so
    two datasets on one revision, a re-run, or two agents sharing a pod hash
    never share a session. It starts with the marker online evaluation
    excludes; a name without it is refused rather than sent as live traffic.
    """
    if not job_name.startswith(HARNESS_SESSION_MARKER):
        raise ValueError(
            f"harness Job name {job_name!r} does not start with {HARNESS_SESSION_MARKER!r}"
        )
    return f"{job_name}-{case_id}"


def build_message_send(text: str, context_id: str, request_id: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "kind": "message",
                "messageId": str(uuid.uuid4()),
                "contextId": context_id,
                "parts": [{"kind": "text", "text": text}],
            }
        },
    }


def _text_of(parts) -> str:
    if not isinstance(parts, list):
        return ""
    return "".join(
        p.get("text", "")
        for p in parts
        if isinstance(p, dict) and p.get("kind") == "text" and isinstance(p.get("text"), str)
    )


def _task_text(result: dict) -> str:
    artifacts = result.get("artifacts")
    if isinstance(artifacts, list) and artifacts:
        return "".join(
            _text_of(a.get("parts")) for a in artifacts if isinstance(a, dict)
        )
    history = result.get("history")
    if isinstance(history, list):
        for message in reversed(history):
            if isinstance(message, dict) and message.get("role") == "agent":
                return _text_of(message.get("parts"))
    return ""


def send_message(
    client: httpx.Client,
    url: str,
    text: str,
    context_id: str,
    timeout: float,
    request_id: str | None = None,
) -> AgentReply:
    body = build_message_send(text, context_id, request_id or context_id)
    try:
        response = client.post(url, json=body, timeout=timeout)
    except httpx.TimeoutException as exc:
        # Connected but the agent did not answer in time. A connect or pool
        # timeout means nothing answered at all: infrastructure, like a
        # refused connection.
        agent_side = not isinstance(exc, (httpx.ConnectTimeout, httpx.PoolTimeout))
        kind = "timeout" if agent_side else "transport"
        raise AgentCallError(kind, f"{type(exc).__name__}: {exc}", agent_side) from exc
    except httpx.HTTPError as exc:
        raise AgentCallError(
            "transport", f"{type(exc).__name__}: {exc}", agent_side=False
        ) from exc
    if not 200 <= response.status_code < 300:
        raise AgentCallError(
            "http",
            f"HTTP {response.status_code}",
            agent_side=response.status_code >= 500,
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise AgentCallError("not-json", "the reply is not JSON") from exc
    if not isinstance(payload, dict):
        raise AgentCallError("not-json", "the reply is not a JSON object")

    # Any error is a case error, even next to a result: a reply that says it
    # failed is never scored.
    error = payload.get("error")
    if payload.get("jsonrpc") == "2.0" and isinstance(error, dict):
        raise AgentCallError(
            "jsonrpc-error",
            f"code {error.get('code')}: {str(error.get('message', ''))[:200]}",
        )
    if error or ("error" in payload and "result" not in payload):
        raise AgentCallError("error-payload", str(error)[:200])

    result = payload.get("result")
    if not isinstance(result, dict):
        raise AgentCallError("error-payload", "the reply has no result object")

    state = None
    if result.get("kind") == "task":
        status = result.get("status") if isinstance(result.get("status"), dict) else {}
        state = status.get("state")
        if state != "completed":
            raise AgentCallError("task-not-completed", f"task state {state!r}")
        reply_text = _task_text(result)
    else:
        reply_text = _text_of(result.get("parts"))

    if not reply_text.strip():
        raise AgentCallError("no-text", "the reply carries no text part")
    return AgentReply(text=reply_text, raw_state=state)


def wait_until_reachable(
    client: httpx.Client,
    base_url: str,
    timeout: float,
    interval: float = 5,
    clock=time.monotonic,
    sleep=time.sleep,
) -> bool:
    """Poll the agent card until it answers 200, or give up at the deadline.

    Service endpoints contain only Ready pods, so a card read through the
    `-preview` Service means a ready pod of the preview ReplicaSet answered.
    """
    base = base_url if base_url.endswith("/") else base_url + "/"
    deadline = clock() + timeout
    while True:
        for path in AGENT_CARD_PATHS:
            try:
                if client.get(base + path, timeout=min(10.0, max(timeout, 1))).status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
        if clock() >= deadline:
            return False
        sleep(interval)
