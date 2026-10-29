"""Strands agent initialization — per-session agents with AgentCore memory."""

import hashlib
import logging
import os
import re
import threading
import time
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from typing import Optional

try:
    from opentelemetry import context as _otel_context
except Exception:  # opentelemetry not installed → context isolation is a no-op
    _otel_context = None

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper()),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

from botocore.exceptions import ClientError
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
from strands import Agent
from strands.agent.agent_result import AgentResult
from strands.models.openai import OpenAIModel
from strands.tools.mcp.mcp_client import MCPClient
from strands.types.exceptions import MaxTokensReachedException
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential, before_sleep_log

try:
    from strands.multiagent.a2a.server import _AGENT_CARD_CONTEXT_ID
except ImportError:
    # Fallback if the SDK renames/removes this internal constant; matches
    # the value as of strands-agents 1.48.0.
    _AGENT_CARD_CONTEXT_ID = "__agent_card__"

from .config import config
from .identity import (
    CALLER_IDENTITY_HEADER,
    WORKLOAD_KEY,
    HeadersProvider,
    caller_key,
    caller_subject,
    is_no_identity_key,
    outbound,
)

logger = logging.getLogger(__name__)

# ── shared resources (created once) ──────────────────────────────────────

_model: Optional[OpenAIModel] = None

# An MCP connection binds its credential when it opens, so connections cannot be
# shared between callers: reusing one would run a caller's tool calls under
# whoever's token opened the connection, and serve them that caller's tool list.
# Pools are therefore keyed by credential (see identity.outbound).
_pools: dict = {}

# The projected ServiceAccount token has a fixed TTL (expirationSeconds,
# currently 1h). The kubelet rewrites the file before it expires, but an open
# connection does not re-read it, so a long-lived workload pool would eventually
# call tools with an expired credential. Recycle the pool well inside that
# lifetime by restarting each MCPClient in place: stop() then start() on the
# same instance. strands' MCPClient is restartable — stop() resets its state
# "to allow instance reuse" and start() re-invokes the transport callable (and
# therefore the headers provider), so the reconnected session carries the
# rotated token without creating new client objects.
#
# In-place restart (rather than rebuilding with fresh client objects) is
# deliberate: the A2A server and cached agents hold tool objects bound to
# specific MCPClient instances. Preserving object identity keeps those
# references valid across a recycle; rebuilding would leave them bound to
# closed clients and every bound tool call would then fail with
# MCPClientInitializationError.
#
# Caller pools need no equivalent: their key is derived from the credential, so a
# refreshed caller token yields a new pool instead of a stale one.
_MCP_CONNECTION_MAX_AGE_SECONDS = 45 * 60

# Cap on pools held open at once; the least recently used is closed past this.
# Each pool costs one connection per configured MCP server.
_MAX_MCP_POOLS = int(os.getenv("MCP_MAX_POOLS", "16"))


class _McpPool:
    """MCP clients and tools for one caller credential.

    `headers` is a provider invoked at connect time, not a fixed dict, so a
    recycled connection re-reads a rotated token.
    """

    def __init__(self, headers: HeadersProvider):
        self.headers = headers
        self.clients: list = []
        self.tools: list = []
        # URLs that successfully connected (populated by _build_client). The
        # /ready probe (mcp_readiness) checks this against the configured set.
        self.connected_urls: set = set()
        self.connected_at: float = 0.0
        # Set by _close. A cached agent still holds this pool's tool objects, so
        # agent_is_stale() tells the caches to rebuild it rather than reuse it.
        self.closed = False
        # Guards clients/tools mutation (the /ready probe thread may run
        # concurrently with the request path).
        self.lock = threading.Lock()


def _is_access_denied(exc: BaseException) -> bool:
    """True if *exc* is a botocore AccessDeniedException (any service)."""
    return isinstance(exc, ClientError) and exc.response.get("Error", {}).get("Code") == "AccessDeniedException"


def _get_model() -> OpenAIModel:
    global _model
    if _model is None:
        # Bifrost is the LLM gateway, exposed as an OpenAI-compatible endpoint
        # at <gateway>/v1. Authentication uses a Bifrost virtual key presented
        # via the `x-bf-vk` header (Bifrost governance). The OpenAI client also
        # requires a non-empty api_key, so we pass the same value there.
        vk = config.LLM_GATEWAY_API_KEY
        _model = OpenAIModel(
            client_args={
                "api_key": vk or "not-used",
                "base_url": config.LLM_GATEWAY_URL,
                "default_headers": {"x-bf-vk": vk},
            },
            model_id=config.MODEL_ID,
            params={
                "max_tokens": config.MAX_TOKENS,
                "stream": True,
                **({"temperature": config.MODEL_TEMPERATURE}
                   if config.MODEL_TEMPERATURE is not None else {}),
            },
        )
    return _model


# ── OTEL context isolation for pooled MCP connections ─────────────────────
# MCPClient.start() spawns a background event loop; anyio copies the *current*
# OTEL context (a ContextVar) into that task and it persists for the whole
# lifetime of the (pooled, cross-request) connection. Every HTTP op the
# background loop later makes — notably the session-teardown DELETE at
# recycle/LRU-close, potentially hours later — is then parented to whatever
# request span happened to be active at start(). Langfuse derives trace
# duration as max(end)-min(start) across observations, so a single stray late
# DELETE glued to the original trace inflates that request's trace to hours.
# Opening and closing the connection under a detached (empty root) context
# keeps those transport spans off the request trace (they become their own
# short root traces), so per-request traces reflect real agent latency.
@contextmanager
def _detached_otel_context():
    if _otel_context is None:  # opentelemetry unavailable → no-op
        yield
        return
    token = _otel_context.attach(_otel_context.Context())
    try:
        yield
    finally:
        _otel_context.detach(token)


# Connecting to an MCP server can fail transiently on a fresh cluster: the pod
# can be scheduled before a backend Deployment or its agentgateway route is
# Ready (commonly a 404 for a route not registered yet, or a 500/503 from the
# gateway). We do NOT retry in-process — _open makes ONE fast attempt per server
# and never raises, so neither startup nor the request path ever blocks. Recovery
# is delegated to Kubernetes via the /ready probe (see mcp_readiness): it
# reconnects any not-yet-connected server every periodSeconds and the pod stays
# out of the Service until its full toolset is up. A 404 is therefore just
# retried on the next probe instead of being treated as permanent.


def _build_client(pool: "_McpPool", url: str) -> MCPClient:
    """Open one MCP connection and append its tools to *pool* (single attempt).

    Runs under a detached OTEL context so the transport's background loop does
    not capture the active request span (see _detached_otel_context). On any
    failure the half-started client is closed so nothing leaks and the error is
    re-raised, so the caller can act on it: _open logs it and moves on;
    mcp_readiness reports it in the /ready 503 body. The pool lock guards the
    clients/tools mutation against the /ready probe thread running concurrently
    with the request path.
    """
    def _transport(u=url, p=pool):
        http_client = create_mcp_http_client(headers=p.headers())
        return streamable_http_client(u, http_client=http_client)

    client = MCPClient(_transport)
    try:
        with _detached_otel_context():
            client.start()
            server_tools = client.list_tools_sync()
    except Exception:
        try:
            with _detached_otel_context():
                client.stop(None, None, None)
        except Exception:
            pass
        raise
    with pool.lock:
        pool.clients.append(client)
        pool.tools.extend(server_tools)
        pool.connected_urls.add(url)
    logger.info(f"  Loaded {len(server_tools)} tools from {url}")
    return client


def _open(pool: "_McpPool", urls: list) -> None:
    """Connect every configured MCP server once. Never raises and never blocks:
    a server that isn't up yet is left unconnected and recovered later by the
    /ready probe (mcp_readiness), which keeps the pod out of the Service until
    the toolset is complete."""
    for url in urls:
        logger.info(f"Connecting to MCP server: {url}")
        try:
            _build_client(pool, url)  # one fast attempt — never blocks
        except Exception as exc:
            logger.warning(f"  MCP server {url} not ready (will retry via /ready probe): {exc}")
    pool.connected_at = time.monotonic()


def _close(pool: _McpPool) -> None:
    pool.closed = True
    for client in pool.clients:
        try:
            # Detach so the session-teardown DELETE is not parented to the
            # caller's request trace (covers transports that issue it inline).
            with _detached_otel_context():
                client.stop(None, None, None)
        except Exception as exc:
            logger.warning(f"  Failed to close MCP connection: {exc}")
    pool.clients = []


# Completeness of the autonomous (workload) toolset is enforced by Kubernetes,
# not in code: the /ready probe (mcp_readiness) keeps the pod out of the Service
# until every configured MCP server is connected on the workload pool, so an
# autonomous request (delivered to the Service by the incident-bridge) only ever
# reaches a pod whose toolset is complete. Chat (caller-keyed) pools degrade
# gracefully. No in-code readiness gate is needed.


def _mcp_server_name(url: str) -> str:
    """Canonical server name for an MCP URL: the segment after '/mcp/' when present,
    else the last non-empty path segment. Used to name a server in the /ready 503
    body (e.g. "gitlab-mcp"). Robust to URLs not shaped as /mcp/<name>."""
    path = url.rstrip("/")
    if "/mcp/" in path:
        return path.rsplit("/mcp/", 1)[-1]
    return path.rsplit("/", 1)[-1]


# Serialises the /ready probe's connect attempts: the kubelet calls /ready every
# periodSeconds and FastAPI runs the sync endpoint in a worker thread, so this
# keeps two overlapping probes from racing to build the same workload pool.
_readiness_lock = threading.Lock()


def _short_reason(exc: BaseException) -> str:
    """One-line failure reason for the /ready 503 body (first line, truncated)."""
    text = str(exc).strip()
    first = text.splitlines()[0] if text else exc.__class__.__name__
    return first[:200]


def mcp_readiness() -> tuple[bool, dict]:
    """Ensure the WORKLOAD MCP pool is fully connected, retrying missing servers.

    Called by the /ready endpoint. For every configured MCP server not yet
    connected on the workload pool — the same pool the autonomous path reuses —
    this makes one connect attempt now, so a Ready pod has the complete toolset.
    Returns ``(ready, reasons)`` where *ready* is True iff every configured
    server is connected and *reasons* maps each still-missing server name to a
    short cause for the 503 body (e.g. ``{"gitlab-mcp": "..."}``). Blocking is
    fine: the endpoint is a sync ``def`` that FastAPI runs off the event loop.
    """
    urls = list(config.MCP_SERVER_URLS)
    if not urls:
        return True, {}
    with _readiness_lock:
        headers, key = outbound(False)  # workload creds; no caller on the probe
        pool = _pools.get(key)
        if pool is None:
            pool = _McpPool(headers)
            pool.connected_at = time.monotonic()
            _pools[key] = pool
        reasons: dict = {}
        for url in urls:
            if url in pool.connected_urls:
                continue
            try:
                _build_client(pool, url)
            except Exception as exc:
                reasons[_mcp_server_name(url)] = _short_reason(exc)
        return (not reasons), reasons


def _get_mcp_tools(key: str, headers: HeadersProvider) -> list:
    """Tools from the MCP pool for *key*, connecting or recycling as needed."""
    pool = _get_mcp_pool(key, headers)
    return pool.tools if pool is not None else []


def _get_mcp_pool(key: str, headers: HeadersProvider) -> Optional[_McpPool]:
    """The MCP pool for *key*, connecting or recycling as needed. None without MCP servers."""
    urls = config.MCP_SERVER_URLS
    if not urls:
        return None

    pool = _pools.pop(key, None)
    if pool is None:
        pool = _McpPool(headers)
        _open(pool, urls)
    elif (
        key == WORKLOAD_KEY
        and time.monotonic() - pool.connected_at >= _MCP_CONNECTION_MAX_AGE_SECONDS
    ):
        logger.info(
            "Recycling %d workload MCP connection(s) to pick up the rotated token",
            len(pool.clients),
        )
        # Restart each client in place (stop() then start() on the SAME
        # instance): start() re-invokes the transport callable so the rotated
        # token is re-read, while object identity is preserved so tool objects
        # held by the A2A server and cached agents stay valid. Detached OTEL
        # context keeps the teardown/reconnect spans off the request trace.
        for client in pool.clients:
            try:
                with _detached_otel_context():
                    client.stop(None, None, None)
                    client.start()
            except Exception as exc:
                logger.warning(f"  Failed to recycle MCP connection: {exc}")
        pool.connected_at = time.monotonic()

    # Re-insert last so dict insertion order doubles as the LRU order.
    _pools[key] = pool
    while len(_pools) > _MAX_MCP_POOLS:
        logger.info("Closing least recently used MCP pool (max %d)", _MAX_MCP_POOLS)
        _close(_pools.pop(next(iter(_pools))))

    # Completeness of the autonomous toolset is enforced by Kubernetes, not here:
    # the /ready probe (mcp_readiness) keeps the pod out of the Service until the
    # workload pool has every configured server, so an autonomous request only
    # reaches a pod whose toolset is complete. Chat pools degrade gracefully.
    return pool


# ── per-session agent creation ───────────────────────────────────────────

# AgentCore accepts actor ids matching (CreateEvent model in botocore):
#   [a-zA-Z0-9][a-zA-Z0-9-_/]*(?::[a-zA-Z0-9-_/]+)*[a-zA-Z0-9-_/]*, max 255
# This is narrower on purpose: "/" is excluded. Long-term retrieval matches
# namespaces by prefix (RetrieveMemoryRecords namespacePath), and namespaces embed
# the actor as /facts/{actorId}/, so an actor "a" would also match the records of
# an actor "a/b". Keycloak subjects (UUIDs) and ServiceAccount subjects
# (system:serviceaccount:<ns>:<name>) match and pass through unchanged.
# Anything else, such as an e-mail-shaped subject from another identity provider,
# becomes "h:<sha256>". Hashing keeps the mapping one-to-one: replacing characters
# would let distinct subjects (a@b.c, a_b.c) share one actor and its memories.
# A subject that itself starts with the hash prefix (in any case) is hashed too, so
# no caller can choose a subject equal to another caller's hashed actor.
_ACTOR_PATTERN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9\-_]*(?::[a-zA-Z0-9\-_]+)*[a-zA-Z0-9\-_]*")


def memory_actor(raw: str) -> str:
    """Map a caller id onto a valid AgentCore actorId, one-to-one."""
    if len(raw) <= 255 and _ACTOR_PATTERN.fullmatch(raw) and not raw.lower().startswith("h:"):
        return raw
    return "h:" + hashlib.sha256(raw.encode()).hexdigest()


# Retrieval defaults per long-term namespace, overridable through MEMORY_CONFIG
# (retrievalTopK, retrievalRelevanceScore). 10 and 0.2 are the SDK's own defaults.
_RETRIEVAL_TOP_K = 10
_RETRIEVAL_RELEVANCE = 0.2

# Strategy namespaces per memory id. Strategies belong to the memory resource, not
# to this agent, so they are read from the service (GetMemory) rather than
# configured twice. Cached for _NAMESPACES_TTL seconds: strategies are created one
# at a time after the memory (AgentCore allows a single update in flight), so an
# agent that starts mid-provisioning must notice the rest a few minutes later
# without a restart.
#
# When a refresh fails, the last good answer is kept for up to _NAMESPACES_MAX_STALE
# (a strategy deleted meanwhile drops out once that runs out), and the next attempt
# waits _NAMESPACES_RETRY, so a sustained AgentCore error costs one call per
# interval rather than one per agent construction.
_NAMESPACES_TTL = 300.0
_NAMESPACES_RETRY = 30.0
_NAMESPACES_MAX_STALE = 3600.0
# memory id -> (fetched_at, namespaces, next_attempt_at)
_namespaces: dict[str, tuple[float, list[tuple[str, Optional[str]]], float]] = {}


def _memory_namespaces(memory_id: str, region: str) -> list[tuple[str, Optional[str]]]:
    """Return [(namespace template, strategy id)] for the memory's strategies.

    Empty when the memory has no long-term strategies, or when they cannot be
    read: the agent then keeps short-term memory only instead of failing.
    """
    now = time.monotonic()
    cached = _namespaces.get(memory_id)

    def last_good() -> list[tuple[str, Optional[str]]]:
        return cached[1] if cached and now - cached[0] < _NAMESPACES_MAX_STALE else []

    if cached and (now - cached[0] < _NAMESPACES_TTL or now < cached[2]):
        return last_good()
    found: list[tuple[str, Optional[str]]] = []
    try:
        from bedrock_agentcore.memory import MemoryClient

        for strategy in MemoryClient(region_name=region).get_memory_strategies(memory_id):
            for ns in strategy.get("namespaces") or []:
                found.append((ns, strategy.get("strategyId")))
    except Exception as e:  # noqa: BLE001 — degrade to short-term memory
        keep = last_good()
        logger.warning(
            "Could not read strategies for memory %s (retry in %ds), using %s: %s",
            memory_id, _NAMESPACES_RETRY, [ns for ns, _ in keep] or "none (long-term retrieval off)", e,
        )
        _namespaces[memory_id] = (cached[0] if cached else 0.0, cached[1] if cached else [], now + _NAMESPACES_RETRY)
        return keep
    _namespaces[memory_id] = (now, found, 0.0)
    if not cached or cached[1] != found:
        logger.info("Memory %s long-term namespaces: %s", memory_id, [ns for ns, _ in found] or "none")
    return found


def _knob(mem_config: dict, key: str, kind, default):
    """A retrieval setting from MEMORY_CONFIG. A typo falls back to the default with a
    warning instead of failing agent construction (the A2A factory runs at startup)."""
    raw = mem_config.get(key)
    if raw in (None, ""):
        return default
    try:
        return kind(raw)
    except (TypeError, ValueError):
        logger.warning("Invalid %s=%r in MEMORY_CONFIG, using %r", key, raw, default)
        return default


def _retrieval_config(memory_id: str, region: str, mem_config: dict) -> Optional[dict]:
    from bedrock_agentcore.memory.integrations.strands.config import RetrievalConfig

    top_k = _knob(mem_config, "retrievalTopK", int, _RETRIEVAL_TOP_K)
    relevance = _knob(mem_config, "retrievalRelevanceScore", float, _RETRIEVAL_RELEVANCE)
    namespaces = _memory_namespaces(memory_id, region)
    if not namespaces:
        return None
    return {
        ns: RetrievalConfig(top_k=top_k, relevance_score=relevance, strategy_id=strategy_id)
        for ns, strategy_id in namespaces
    }


# AgentCore Memory constrains sessionId / actorId to
# ``[a-zA-Z0-9][a-zA-Z0-9-_]*`` with a maximum length of 100. The contextId we
# receive from the request body is caller-supplied and, for autonomous
# incidents, is derived from an alert fingerprint (e.g.
# "PodOOMKilled|spoke-dev|ns|pod|hog") — it contains '|' and can exceed 100
# characters, so passing it verbatim makes every ListEvents/CreateEvent call
# fail with a ValidationException and the agent never completes the RCA.
_AGENTCORE_ID_MAX_LEN = 100
_AGENTCORE_ID_INVALID = re.compile(r"[^A-Za-z0-9_-]")


def _sanitize_agentcore_id(value: str) -> str:
    """Coerce an arbitrary id into a valid AgentCore sessionId / actorId.

    Invalid characters become '-'; the result is guaranteed to start with an
    alphanumeric character and to be at most ``_AGENTCORE_ID_MAX_LEN`` chars.
    A short deterministic hash of the original is appended whenever sanitizing
    changes the id (not only on truncation), so distinct inputs that collapse to
    the same cleaned string keep distinct ids (no memory cross-talk). An
    already-valid id is returned byte-identical.
    """
    original = value or ""
    cleaned = _AGENTCORE_ID_INVALID.sub("-", original)
    changed = cleaned != original
    if not cleaned or not cleaned[0].isalnum():
        cleaned = "s-" + cleaned.lstrip("-_")
        changed = True
    if changed or len(cleaned) > _AGENTCORE_ID_MAX_LEN:
        digest = hashlib.sha256(original.encode("utf-8")).hexdigest()[:12]
        cleaned = cleaned[: _AGENTCORE_ID_MAX_LEN - 1 - len(digest)].rstrip("-_") + "-" + digest
    return cleaned


def _build_session_manager(session_id: str, actor_id: Optional[str]):
    """Build an AgentCoreMemorySessionManager for a specific session.

    Short-term memory (the conversation, keyed by session) is on when a memory is
    configured and the caller is known. Long-term retrieval is on when the memory
    resource has strategies; the developer opts in on the agentcore-memory component.

    No caller (no gateway identity header) means no memory at all: such requests did
    not come through the gateway, and pooling them under one actor would share their
    memories with each other.
    """
    if config.MEMORY_PROVIDER != "agentcore":
        return None

    # The A2AServer agent_factory is invoked once at construction with a
    # placeholder context id ("__agent_card__") solely to derive agent-card
    # metadata; that agent is never used for request handling. Skip memory
    # attachment for it — AgentCore session ids must start with an
    # alphanumeric character, which the placeholder does not satisfy.
    if session_id == _AGENT_CARD_CONTEXT_ID:
        return None

    mem_config = config.MEMORY_CONFIG
    memory_id = mem_config.get("memoryId")
    region = mem_config.get("region", config.AWS_REGION)

    if not memory_id:
        logger.warning("MEMORY_PROVIDER=agentcore but no memoryId in MEMORY_CONFIG")
        return None

    if not actor_id:
        logger.warning(
            "No caller identity (%s header) on this request; memory is off for session %r",
            CALLER_IDENTITY_HEADER, session_id,
        )
        return None

    from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig
    from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager

    # Session id: sanitized (AgentCore SessionId is [a-zA-Z0-9][a-zA-Z0-9-_]*, max 100; the
    # raw contextId may carry '|' or be longer, e.g. autonomous-incident fingerprints).
    # The raw session_id is still what we cache and echo back as contextId.
    # Actor id: memory_actor, not the session sanitizer. ActorId allows ':' and 255 chars,
    # so ServiceAccount subjects pass through, and memory_actor keeps the caller mapping
    # one-to-one (see above).
    safe_session_id = _sanitize_agentcore_id(session_id)
    actor = memory_actor(actor_id)
    agentcore_config = AgentCoreMemoryConfig(
        memory_id=memory_id,
        session_id=safe_session_id,
        actor_id=actor,
        retrieval_config=_retrieval_config(memory_id, region, mem_config),
    )
    sm = AgentCoreMemorySessionManager(
        agentcore_memory_config=agentcore_config,
        region_name=region,
    )
    logger.info(
        "AgentCore session manager created (memory=%s, session=%s, actor=%s, long_term=%s, raw_context=%r)",
        memory_id, safe_session_id, actor, bool(agentcore_config.retrieval_config), session_id,
    )
    return sm


# ── Langfuse/OTEL trace attributes ───────────────────────────────────────
# Strands applies these to the agent's trace span; Langfuse lifts the
# well-known keys to trace level: ``session.id`` -> Session view (groups every
# turn of one conversation / one incident), ``user.id`` -> User, ``tags`` ->
# filterable tags. We derive the source from the session id rather than a
# separate flag: the incident-bridge sets the A2A contextId to
# "incident-<fingerprint>" for autonomous RCA and leaves it caller-supplied
# (or a fresh UUID) for interactive chat — so a "incident-" prefix is a
# reliable, transport-agnostic discriminator. This lets Langfuse filter
# tags=source:rca vs source:chat in one click, which neither session_id nor
# name filtering could do before (session_id was empty and untagged).
#
# Memory for that autonomous path: the bridge posts to the agent's Service
# directly (see the readiness note near _mcp_server_name), not through the gateway,
# so it carries no X-Forwarded-User. Such a request fails closed: no long-term
# memory, and no agent kept across turns even with a stable incident-<fingerprint>
# contextId. A bridge that needs either must call through the gateway with its own
# ServiceAccount token, which the gateway turns into a stable subject.
def _trace_attributes(session_id: str, actor_id: str) -> dict:
    source = "rca" if (session_id or "").startswith("incident-") else "chat"
    tags = [f"source:{source}", config.AGENT_NAME]
    return {
        # Raw session id (what we echo back as contextId) so the Langfuse
        # Session groups by conversation/incident. Langfuse has no AgentCore
        # charset constraint, so the raw value is fine here.
        "session.id": session_id or "",
        "user.id": actor_id or "user",
        # Langfuse lifts trace tags ONLY from "langfuse.trace.tags" (verified
        # empirically: a bare "tags" attribute is ignored, while session.id /
        # user.id ARE accepted as fallbacks). Keep "tags" too — inert on this
        # Langfuse version but forward-compatible and harmless.
        "langfuse.trace.tags": tags,
        "tags": tags,
    }


# ── graceful max_tokens handling ──────────────────────────────────────────
# When a generation hits the per-request output cap, Strands' event loop raises
# MaxTokensReachedException *after* it has already streamed the partial answer.
# Both consumers of the agent — the A2A executor (the chat UI / incident-bridge
# path) and invoke_async (the /chat endpoint) — let that exception propagate:
# the A2A task then transitions to `failed` with the opaque text "Agent
# execution failed" and the chat shows the answer cut off mid-sentence with no
# reason; /chat returns {"error": ...}. The user cannot tell truncation from a
# real crash.
#
# This guard wraps the agent's own stream_async (which invoke_async also drives,
# so one wrap covers both paths). On MaxTokensReachedException it does NOT
# re-raise: it emits one more text chunk (config.MAX_TOKENS_NOTICE) so the chat
# shows *why* the answer stopped, then yields a terminal AgentResult
# (stop_reason="max_tokens") built from the text already streamed this turn.
# The A2A executor sees a normal result and marks the task `completed`;
# invoke_async returns an AgentResult whose __str__ is the partial answer plus
# the notice. Any *other* exception still propagates and still fails the task.
def _install_truncation_guard(agent: Agent) -> None:
    """Turn an unhandled max_tokens truncation into a graceful, visible notice."""
    if not config.MAX_TOKENS_NOTICE_ENABLED:
        return

    original_stream = agent.stream_async
    notice = config.MAX_TOKENS_NOTICE

    async def _guarded(*args, **kwargs):
        buffered: list[str] = []
        try:
            async for event in original_stream(*args, **kwargs):
                if isinstance(event, dict) and isinstance(event.get("data"), str):
                    buffered.append(event["data"])
                yield event
        except MaxTokensReachedException:
            logger.warning(
                "Response truncated: max_tokens=%s reached mid-generation; "
                "finishing turn with a visible notice instead of failing the task.",
                config.MAX_TOKENS,
            )
            # 1) stream the notice so the UI renders it as the tail of the answer
            yield {"data": "\n\n" + notice}
            # 2) emit a terminal result so invoke_async returns and the A2A
            #    executor completes (not fails) the task. message carries the
            #    partial text already streamed this turn + the notice, so
            #    str(result) (used by /chat) renders the full partial answer.
            partial_text = ("".join(buffered) + "\n\n" + notice).strip()
            message = {"role": "assistant", "content": [{"text": partial_text}]}
            yield {
                "result": AgentResult(
                    stop_reason="max_tokens",
                    message=message,
                    metrics=agent.event_loop_metrics,
                    state={},
                )
            }

    agent.stream_async = _guarded


@retry(
    retry=retry_if_exception(_is_access_denied),
    wait=wait_exponential(multiplier=1, max=16),
    stop=stop_after_attempt(6),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
def _construct_agent(session_id: str, actor_id: str) -> Agent:
    """Build the session manager + Agent.

    Retries on AccessDeniedException (first-boot IAM propagation race
    between Pod Identity association and the AgentCore access policy)
    with exponential backoff instead of crashing the process.
    """
    session_manager = _build_session_manager(session_id, actor_id)
    headers, key = outbound(config.PROPAGATE_CALLER_TOKEN)
    pool = _get_mcp_pool(key, headers)
    tools = (pool.tools if pool is not None else None) or None
    agent = Agent(
        model=_get_model(),
        system_prompt=config.SYSTEM_PROMPT,
        tools=tools,
        agent_id=config.AGENT_NAME,
        name=config.AGENT_NAME,
        description=config.AGENT_DESCRIPTION,
        session_manager=session_manager,
        trace_attributes=_trace_attributes(session_id, actor_id),
    )
    _install_truncation_guard(agent)
    agent._oap_mcp_pool = pool
    return agent


def agent_is_stale(agent) -> bool:
    """True when the MCP pool this agent's tools belong to has been closed.

    Pools are capped (MCP_MAX_POOLS) separately from the agent caches
    (MAX_CACHED_AGENTS, the A2A executor's max_contexts). When the caller's token is
    forwarded, each caller has its own pool, so a pool can be evicted while an agent
    built on it is still cached; reusing that agent would call stopped clients. Both
    caches check this on a hit and rebuild instead, which also reopens the pool.
    """
    pool = getattr(agent, "_oap_mcp_pool", None)
    return pool is not None and pool.closed


def create_agent(session_id: Optional[str] = None, actor_id: Optional[str] = None) -> Agent:
    """Create a Strands agent for a given session.

    Also the A2AServer agent_factory, which calls it with the context id only.

    Args:
        session_id: Conversation session id. A new UUID is generated when None.
        actor_id: Memory actor. Defaults to the caller identity the gateway set on
            this request (identity.caller_subject), so memory is per caller. None
            when the request carries no identity: the agent then has no memory.
    """
    session_id = session_id or str(uuid.uuid4())
    actor_id = actor_id or caller_subject()
    agent = _construct_agent(session_id, actor_id)
    logger.info(f"Agent created: {config.AGENT_NAME} session={session_id}")
    return agent


# ── session cache ────────────────────────────────────────────────────────

# Bounded LRU, like the A2A executor's own context cache (same default bound). An
# evicted agent is only dropped from this cache: its conversation is in AgentCore
# when memory is on, and its MCP connections live in the separate, bounded pools.
# Requests with no identity are never cached (their key is unique to the request),
# and an agent whose MCP pool was closed is rebuilt on its next hit (agent_is_stale).
_MAX_AGENTS = int(os.getenv("MAX_CACHED_AGENTS", "1000"))
_agents: "OrderedDict[tuple, Agent]" = OrderedDict()


def get_or_create_agent(session_id: Optional[str] = None, actor_id: Optional[str] = None) -> tuple[Agent, str]:
    """Return a cached agent for *session_id*, creating one if needed.

    Cached per (caller, session) rather than per session alone. `session_id`
    arrives from the request body as `contextId`, so keying on it alone would let
    one caller retrieve another caller's agent, whose MCP connections carry that
    caller's credential, by supplying a known context id.

    Returns (agent, session_id).
    """
    # Key on the INBOUND caller (identity.caller_key), never on the outbound
    # credential alone: with PROPAGATE_CALLER_TOKEN=false that is the constant
    # workload key, which would let two users who send the same contextId share one
    # agent and its conversation history.
    caller = caller_key(config.PROPAGATE_CALLER_TOKEN)

    if session_id and (caller, session_id) in _agents:
        cached = _agents[(caller, session_id)]
        if not agent_is_stale(cached):
            _agents.move_to_end((caller, session_id))
            return cached, session_id
        del _agents[(caller, session_id)]

    sid = session_id or str(uuid.uuid4())
    agent = create_agent(session_id=sid, actor_id=actor_id)
    if is_no_identity_key(caller):
        # The key is unique to this request, so no later request can look it up.
        # Caching it would only evict the agents of callers who do have an identity.
        return agent, sid
    _agents[(caller, sid)] = agent
    while len(_agents) > _MAX_AGENTS:
        _agents.popitem(last=False)
    return agent, sid


# ── cleanup ──────────────────────────────────────────────────────────────

def shutdown_mcp() -> None:
    if _pools:
        logger.info("Closing MCP client connections for %d pool(s)", len(_pools))
        while _pools:
            _close(_pools.pop(next(iter(_pools))))
