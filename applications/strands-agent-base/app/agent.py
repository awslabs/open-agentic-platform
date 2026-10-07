"""Strands agent initialization — per-session agents with AgentCore memory."""

import hashlib
import logging
import os
import re
import time
import uuid
from typing import Optional

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper()),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

from botocore.exceptions import ClientError
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
from strands import Agent
from strands.models.openai import OpenAIModel
from strands.tools.mcp.mcp_client import MCPClient
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential, before_sleep_log

try:
    from strands.multiagent.a2a.server import _AGENT_CARD_CONTEXT_ID
except ImportError:
    # Fallback if the SDK renames/removes this internal constant; matches
    # the value as of strands-agents 1.48.0.
    _AGENT_CARD_CONTEXT_ID = "__agent_card__"

from .config import config
from .identity import WORKLOAD_KEY, HeadersProvider, caller_actor, caller_key, outbound

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
# call tools with an expired credential. Recycle in place (stop() + start() on the
# same MCPClient, which re-invokes the transport callable and therefore the
# headers provider) well inside that lifetime; reusing the instances keeps
# already-built Agents' tool objects valid, since those bind to MCPClient object
# identity rather than a point-in-time session.
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
        self.connected_at: float = 0.0


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
            params={"max_tokens": 1000, "temperature": 0.7, "stream": True},
        )
    return _model


def _open(pool: _McpPool, urls: list) -> None:
    for url in urls:
        logger.info(f"Connecting to MCP server: {url}")
        try:
            # streamable_http_client no longer takes a `headers` kwarg directly
            # (mcp SDK, pulled in via strands-agents 1.57.0's bump): headers now
            # travel on a pre-built httpx client passed as `http_client`.
            client = MCPClient(
                lambda u=url, p=pool: streamable_http_client(
                    u, http_client=create_mcp_http_client(headers=p.headers())
                )
            )
            client.start()
            server_tools = client.list_tools_sync()
            logger.info(f"  Loaded {len(server_tools)} tools from {url}")
            pool.clients.append(client)
            pool.tools.extend(server_tools)
        except Exception as exc:
            logger.warning(f"  Failed to connect to MCP server {url}: {exc}")
    pool.connected_at = time.monotonic()


def _close(pool: _McpPool) -> None:
    for client in pool.clients:
        try:
            client.stop(None, None, None)
        except Exception as exc:
            logger.warning(f"  Failed to close MCP connection: {exc}")
    pool.clients = []


def _get_mcp_tools(key: str, headers: HeadersProvider) -> list:
    """Tools from the MCP pool for *key*, connecting or recycling as needed."""
    urls = config.MCP_SERVER_URLS
    if not urls:
        return []

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
        for client in pool.clients:
            try:
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

    return pool.tools


# ── per-session agent creation ───────────────────────────────────────────

# Memory actor when no caller identity header arrived (see identity.caller_actor).
# Such requests did not come through the gateway, so they share one actor.
DEFAULT_ACTOR = "anonymous"

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
# The reserved values (the hash prefix and the anonymous actor) are hashed too, so
# a caller cannot choose a subject that equals another actor.
_ACTOR_PATTERN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9\-_]*(?::[a-zA-Z0-9\-_]+)*[a-zA-Z0-9\-_]*")


def memory_actor(raw: str) -> str:
    """Map a caller id onto a valid AgentCore actorId, one-to-one."""
    if len(raw) <= 255 and _ACTOR_PATTERN.fullmatch(raw) and not raw.startswith("h:") and raw != DEFAULT_ACTOR:
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
_NAMESPACES_TTL = 300.0
_namespaces: dict[str, tuple[float, list[tuple[str, Optional[str]]]]] = {}


def _memory_namespaces(memory_id: str, region: str) -> list[tuple[str, Optional[str]]]:
    """Return [(namespace template, strategy id)] for the memory's strategies.

    Empty when the memory has no long-term strategies, or when they cannot be
    read: the agent then keeps short-term memory only instead of failing.
    """
    cached = _namespaces.get(memory_id)
    if cached and time.monotonic() - cached[0] < _NAMESPACES_TTL:
        return cached[1]
    found: list[tuple[str, Optional[str]]] = []
    try:
        from bedrock_agentcore.memory import MemoryClient

        for strategy in MemoryClient(region_name=region).get_memory_strategies(memory_id):
            for ns in strategy.get("namespaces") or []:
                found.append((ns, strategy.get("strategyId")))
    except Exception as e:  # noqa: BLE001 — degrade to short-term memory
        logger.warning("Could not read strategies for memory %s, long-term retrieval off: %s", memory_id, e)
        return cached[1] if cached else found  # keep the last good answer; retry next session
    _namespaces[memory_id] = (time.monotonic(), found)
    if not cached or cached[1] != found:
        logger.info("Memory %s long-term namespaces: %s", memory_id, [ns for ns, _ in found] or "none")
    return found


def _retrieval_config(memory_id: str, region: str, mem_config: dict) -> Optional[dict]:
    from bedrock_agentcore.memory.integrations.strands.config import RetrievalConfig

    top_k = int(mem_config.get("retrievalTopK", _RETRIEVAL_TOP_K))
    relevance = float(mem_config.get("retrievalRelevanceScore", _RETRIEVAL_RELEVANCE))
    namespaces = _memory_namespaces(memory_id, region)
    if not namespaces:
        return None
    return {
        ns: RetrievalConfig(top_k=top_k, relevance_score=relevance, strategy_id=strategy_id)
        for ns, strategy_id in namespaces
    }


def _build_session_manager(session_id: str, actor_id: str):
    """Build an AgentCoreMemorySessionManager for a specific session.

    Short-term memory (the conversation, keyed by session) is always on when a
    memory is configured. Long-term retrieval is on when the memory resource has
    strategies; the developer opts in on the agentcore-memory component.
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

    from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig
    from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager

    actor = memory_actor(actor_id)
    agentcore_config = AgentCoreMemoryConfig(
        memory_id=memory_id,
        session_id=session_id,
        actor_id=actor,
        retrieval_config=_retrieval_config(memory_id, region, mem_config),
    )
    sm = AgentCoreMemorySessionManager(
        agentcore_memory_config=agentcore_config,
        region_name=region,
    )
    logger.info(
        "AgentCore session manager created (memory=%s, session=%s, actor=%s, long_term=%s)",
        memory_id, session_id, actor, bool(agentcore_config.retrieval_config),
    )
    return sm


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
    tools = _get_mcp_tools(key, headers) or None
    return Agent(
        model=_get_model(),
        system_prompt=config.SYSTEM_PROMPT,
        tools=tools,
        agent_id=config.AGENT_NAME,
        name=config.AGENT_NAME,
        description=config.AGENT_DESCRIPTION,
        session_manager=session_manager,
    )


def create_agent(session_id: Optional[str] = None, actor_id: Optional[str] = None) -> Agent:
    """Create a Strands agent for a given session.

    Also the A2AServer agent_factory, which calls it with the context id only.

    Args:
        session_id: Conversation session id. A new UUID is generated when None.
        actor_id: Memory actor. Defaults to the caller identity the gateway set on
            this request (identity.caller_actor), so long-term memory is per caller.
    """
    session_id = session_id or str(uuid.uuid4())
    actor_id = actor_id or caller_actor(DEFAULT_ACTOR)
    agent = _construct_agent(session_id, actor_id)
    logger.info(f"Agent created: {config.AGENT_NAME} session={session_id}")
    return agent


# ── session cache ────────────────────────────────────────────────────────

_agents: dict[tuple, Agent] = {}


def get_or_create_agent(session_id: Optional[str] = None, actor_id: Optional[str] = None) -> tuple[Agent, str]:
    """Return a cached agent for *session_id*, creating one if needed.

    Cached per (caller, session) rather than per session alone. `session_id`
    arrives from the request body as `contextId`, so keying on it alone would let
    one caller retrieve another caller's agent, whose MCP connections carry that
    caller's credential, by supplying a known context id.

    Returns (agent, session_id).
    """
    # Key on the INBOUND caller, whatever credential goes OUTBOUND. With
    # PROPAGATE_CALLER_TOKEN=false the outbound key is the constant workload key,
    # so keying on it let two users who send the same contextId share one agent
    # and its conversation history.
    caller = caller_key()

    if session_id and (caller, session_id) in _agents:
        return _agents[(caller, session_id)], session_id

    sid = session_id or str(uuid.uuid4())
    agent = create_agent(session_id=sid, actor_id=actor_id)
    _agents[(caller, sid)] = agent
    return agent, sid


# ── cleanup ──────────────────────────────────────────────────────────────

def shutdown_mcp() -> None:
    if _pools:
        logger.info("Closing MCP client connections for %d pool(s)", len(_pools))
        while _pools:
            _close(_pools.pop(next(iter(_pools))))
