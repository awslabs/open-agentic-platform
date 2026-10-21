"""Delegation to other agents (supervisor pattern).

    user --> gateway --> supervisor --[own token, X-Delegated-User]--> gateway --> sub-agent

An agent configured with A2A_AGENT_NAMES (the OAM `agents` list) gets one tool per
listed agent. The tool's description comes from that agent's A2A agent card, fetched
through the gateway, so the model picks a sub-agent the same way it picks an MCP tool.
Calling the tool sends the subtask to the sub-agent as an A2A message/send and returns
its reply. Design: docs/agent-delegation/DESIGN.md.

Identity on the delegated call:
  Authorization       the supervisor's own ServiceAccount token (as for MCP), so the
                      gateway's agentAccess rules see which agent is calling
  X-Delegated-User    the user this request is for (identity.caller_subject). The gateway
                      turns it into the sub-agent's X-Forwarded-User, but only for agents
                      in its delegation.agents list, so the sub-agent's memory and A2A
                      isolation stay per user. Omitted when this request has no user.
  X-Delegation-Depth  this call's depth, so a chain of agents stops at MAX_DELEGATION_DEPTH

Cards are fetched off the request path: once synchronously when the first agent is
built (at import, before the server takes requests), then by a background thread every
DELEGATION_CARD_TTL_SECONDS (failed ones every 30s). Building an agent only reads that
cache, so a slow or missing sub-agent never blocks a request. A sub-agent whose card
cannot be fetched (not deployed, not ready, or denied by agentAccess) is left out and
logged; the agent still starts. When the set of reachable sub-agents changes, agents
built on the old set are stale (agent.agent_is_stale) and are rebuilt on their next
request, so a sub-agent that comes up later reaches existing sessions too.

Outbound HTTP goes through httpx, which main.py instruments, so the trace context
reaches the sub-agent and the traces join.
"""

import logging
import os
import re
import threading
import time
import uuid
from collections import OrderedDict
from typing import Optional

import httpx
from strands.tools.tools import PythonAgentTool

from .config import config
from .identity import (
    DELEGATED_USER_HEADER,
    DELEGATION_DEPTH_HEADER,
    caller_subject,
    inbound_depth,
    request_id,
    workload_headers,
)

logger = logging.getLogger(__name__)

_CARD_PATH = "/.well-known/agent-card.json"  # a2a-sdk AGENT_CARD_WELL_KNOWN_PATH
_CARD_TTL = float(os.getenv("DELEGATION_CARD_TTL_SECONDS", "300"))
_CARD_RETRY = 30.0  # a failed fetch is retried sooner than a good card is refreshed
_CARD_TIMEOUT = 10.0
MAX_DEPTH = int(os.getenv("MAX_DELEGATION_DEPTH", "3"))
_CALL_TIMEOUT = float(os.getenv("DELEGATION_TIMEOUT_SECONDS", "120"))
_MAX_PER_REQUEST = int(os.getenv("MAX_DELEGATIONS_PER_REQUEST", "10"))

_TOOL_NAME_INVALID = re.compile(r"[^a-zA-Z0-9_-]")

# name -> (fetched_at, card dict or None, reason when None)
_cards: dict[str, tuple[float, Optional[dict], str]] = {}
_cards_lock = threading.Lock()

# request id -> delegations made while serving it. Bounded: entries for finished
# requests are only ever evicted, never read again.
_counts: "OrderedDict[str, int]" = OrderedDict()
_counts_lock = threading.Lock()
_MAX_TRACKED_REQUESTS = 1000


def agent_names() -> list[str]:
    """Configured sub-agents, in order, without duplicates or this agent itself."""
    seen, out = set(), []
    for name in (s.strip() for s in (os.getenv("A2A_AGENT_NAMES") or "").split(",")):
        if name and name != config.AGENT_NAME and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def agent_url(name: str) -> str:
    """The sub-agent's route on this cluster's gateway (the agent component's /<name>)."""
    return f"{config.GATEWAY_URL}/{name}/"


def _fetch_card(name: str) -> tuple[Optional[dict], str]:
    try:
        resp = httpx.get(agent_url(name).rstrip("/") + _CARD_PATH, headers=workload_headers(), timeout=_CARD_TIMEOUT)
    except httpx.HTTPError as exc:
        return None, f"{type(exc).__name__}: {exc}"[:200]
    if resp.status_code != 200:
        # 403: agentAccess does not list this agent; 404/503: not deployed or not ready.
        return None, f"HTTP {resp.status_code}"
    try:
        card = resp.json()
    except ValueError:
        return None, "card is not JSON"
    if not isinstance(card, dict):
        return None, "card is not a JSON object"
    return card, ""


def _due(name: str, now: float) -> bool:
    with _cards_lock:
        hit = _cards.get(name)
    if hit is None:
        return True
    fetched_at, cached, _ = hit
    return now - fetched_at >= (_CARD_TTL if cached is not None else _CARD_RETRY)


def refresh(force: bool = False) -> None:
    """Fetch every configured card that is due (all of them when *force*)."""
    now = time.monotonic()
    for name in agent_names():
        if force or _due(name, now):
            _refresh_one(name)


def _refresh_one(name: str) -> None:
    now = time.monotonic()
    with _cards_lock:
        hit = _cards.get(name)
    fetched, reason = _fetch_card(name)
    if fetched is None and hit is not None and hit[1] is not None:
        # Keep the last good card through a transient failure: the call itself will
        # report the error if the agent is really gone.
        logger.warning("Agent card refresh for %s failed (%s); keeping the previous card", name, reason)
        fetched, reason = hit[1], ""
    with _cards_lock:
        _cards[name] = (now, fetched, reason)
    if fetched is None:
        logger.warning("Sub-agent %s left out: agent card unavailable (%s)", name, reason)


_refresher_lock = threading.Lock()
_refresher_started = False


def _refresh_loop() -> None:
    while True:
        time.sleep(min(_CARD_RETRY, _CARD_TTL))
        try:
            refresh()
        except Exception:  # never let the refresher die
            logger.exception("Agent card refresh failed")


def ensure_refresher() -> None:
    """First call: fetch all cards now, then keep them fresh in a daemon thread."""
    global _refresher_started
    if not agent_names():
        return
    with _refresher_lock:
        if _refresher_started:
            return
        _refresher_started = True
    if not os.getenv("WORKLOAD_TOKEN_PATH"):
        logger.error(
            "A2A_AGENT_NAMES is set but the agent has no gateway token (WORKLOAD_TOKEN_PATH): "
            "add the gateway-identity trait, or every sub-agent card fetch is rejected"
        )
    refresh(force=True)
    threading.Thread(target=_refresh_loop, name="agent-card-refresh", daemon=True).start()


def card(name: str) -> tuple[Optional[dict], str]:
    """The cached card for *name*; (None, reason) when it has not been fetched."""
    with _cards_lock:
        hit = _cards.get(name)
    return (hit[1], hit[2]) if hit is not None else (None, "not fetched yet")


def generation() -> tuple:
    """Identifies the set of reachable sub-agents; changes when one appears or goes."""
    return tuple(n for n in agent_names() if card(n)[0] is not None)


def tool_name(name: str) -> str:
    return ("ask_" + _TOOL_NAME_INVALID.sub("_", name))[:64]


def _describe(name: str, card_: dict) -> str:
    lines = [f"Delegate a task to the agent '{name}'. {card_.get('description') or ''}".strip()]
    skills = [s for s in card_.get("skills") or [] if isinstance(s, dict)]
    if skills:
        lines.append("Its skills:")
        for s in skills[:20]:
            text = f"- {s.get('name') or s.get('id')}: {s.get('description') or ''}".rstrip(": ")
            lines.append(text[:300])
    lines.append(
        "Pass a self-contained task; the agent does not see this conversation. "
        "Returns the agent's answer as text."
    )
    return "\n".join(lines)[:4000]


def _count_call() -> bool:
    """Record one delegation for this request; False once the per-request cap is hit."""
    key = request_id.get() or "-"
    with _counts_lock:
        n = _counts.get(key, 0) + 1
        _counts[key] = n
        _counts.move_to_end(key)
        while len(_counts) > _MAX_TRACKED_REQUESTS:
            _counts.popitem(last=False)
    return n <= _MAX_PER_REQUEST


def outbound_headers() -> dict:
    """Headers for a delegated call, built from the request in flight."""
    headers = dict(workload_headers())
    user = caller_subject()
    if user:
        headers[DELEGATED_USER_HEADER] = user
    headers[DELEGATION_DEPTH_HEADER] = str(inbound_depth() + 1)
    return headers


def _texts(parts) -> list[str]:
    return [p.get("text") or "" for p in parts or [] if isinstance(p, dict) and p.get("kind") == "text"]


def _reply_text(body) -> tuple[bool, str]:
    """(ok, text) from an A2A JSON-RPC message/send response (a Task or a Message)."""
    if not isinstance(body, dict):
        return False, "the agent's reply was not a JSON-RPC object"
    if body.get("error"):
        err = body["error"] if isinstance(body["error"], dict) else {"message": str(body["error"])}
        return False, f"A2A error {err.get('code')}: {err.get('message')}"
    result = body.get("result")
    if not isinstance(result, dict):
        return False, "the agent returned no result"
    texts = []
    for artifact in result.get("artifacts") or []:
        if isinstance(artifact, dict):
            texts += _texts(artifact.get("parts"))
    texts += _texts(result.get("parts"))  # a Message result
    status = result.get("status") if isinstance(result.get("status"), dict) else {}
    message = status.get("message") if isinstance(status.get("message"), dict) else {}
    status_text = "".join(_texts(message.get("parts")))
    state = status.get("state")
    text = "".join(t for t in texts if t) or status_text
    if state in ("failed", "rejected", "canceled"):
        return False, f"the agent's task ended {state}" + (f": {text}" if text else "")
    if state in ("input-required", "auth-required"):
        return False, f"the agent needs {state.split('-')[0]} it cannot get through delegation" + (f": {text}" if text else "")
    return (True, text) if text else (False, "the agent returned no text")


def _make_tool(name: str, card_: dict, session_id: str) -> PythonAgentTool:
    # One sub-agent context per (supervisor session, sub-agent), so follow-ups in the
    # same supervisor session continue that sub-agent's thread.
    context_id = f"{session_id}-{name}"
    spec = {
        "name": tool_name(name),
        "description": _describe(name, card_),
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {"task": {"type": "string", "description": "The task for the agent, self-contained."}},
                "required": ["task"],
            }
        },
    }

    async def call(tool_use, **_):
        use_id = tool_use["toolUseId"]

        def result(ok: bool, text: str) -> dict:
            return {"toolUseId": use_id, "status": "success" if ok else "error", "content": [{"text": text}]}

        task = ((tool_use.get("input") or {}).get("task") or "").strip()
        if not task:
            return result(False, "No task given.")
        if inbound_depth() + 1 > MAX_DEPTH:
            logger.warning("Delegation to %s refused: depth limit %d reached", name, MAX_DEPTH)
            return result(False, f"Delegation depth limit ({MAX_DEPTH}) reached; answer without delegating further.")
        if not _count_call():
            return result(False, f"Delegation limit for this request ({_MAX_PER_REQUEST}) reached.")
        payload = {
            "jsonrpc": "2.0",
            "id": uuid.uuid4().hex,
            "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "messageId": uuid.uuid4().hex,
                    "contextId": context_id,
                    "parts": [{"kind": "text", "text": task}],
                }
            },
        }
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=_CALL_TIMEOUT) as client:
                resp = await client.post(agent_url(name), json=payload, headers=outbound_headers())
        except httpx.TimeoutException:
            logger.warning("Delegation to %s timed out after %.0fs", name, _CALL_TIMEOUT)
            return result(False, f"The agent '{name}' did not answer within {_CALL_TIMEOUT:.0f}s.")
        except httpx.HTTPError as exc:
            return result(False, f"The agent '{name}' could not be reached: {type(exc).__name__}.")
        if resp.status_code != 200:
            logger.warning("Delegation to %s failed: HTTP %d", name, resp.status_code)
            return result(False, f"The agent '{name}' returned HTTP {resp.status_code}.")
        try:
            ok, text = _reply_text(resp.json())
        except ValueError:
            ok, text = False, "the agent's reply was not JSON"
        logger.info(
            "Delegated to %s: ok=%s depth=%d elapsed=%.1fs context=%s",
            name, ok, inbound_depth() + 1, time.monotonic() - started, context_id,
        )
        return result(ok, text if ok else f"The agent '{name}' failed: {text}")

    return PythonAgentTool(spec["name"], spec, call)


def delegation_tools(session_id: str) -> tuple[list, list[str], list[str]]:
    """(tools, available names, unavailable names) for an agent serving *session_id*.

    Reads the card cache only; never fetches on the request path.
    """
    ensure_refresher()
    tools, available, unavailable = [], [], []
    for name in agent_names():
        card_, _ = card(name)
        if card_ is None:
            unavailable.append(name)
            continue
        tools.append(_make_tool(name, card_, session_id))
        available.append(name)
    if available or unavailable:
        logger.info(
            "Sub-agents for session %s: available=%s unavailable=%s",
            session_id, ",".join(available) or "-", ",".join(unavailable) or "-",
        )
    return tools, available, unavailable


def prompt_addendum(available: list[str], unavailable: list[str]) -> str:
    """Instructions appended to the system prompt of an agent that has sub-agents."""
    if not available and not unavailable:
        return ""
    lines = [
        "",
        "You can delegate to other agents through the ask_<agent> tools. To meet the goal above:",
        "plan the steps it needs; give each step to the agent whose description fits it, as a",
        "self-contained task; call agents whose steps do not depend on each other in the same",
        "turn so they run at the same time; then combine their answers and check the result",
        "against the goal before you reply. If an agent fails, say what is missing rather",
        "than guessing.",
    ]
    if unavailable:
        lines.append("These agents are currently unavailable: " + ", ".join(unavailable) + ".")
    return "\n".join(lines)
