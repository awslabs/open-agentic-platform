"""Strands agent initialization — per-session agents with AgentCore memory."""

import logging
import os
import time
import uuid
from typing import Optional

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper()),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

from botocore.exceptions import ClientError
from mcp.client.streamable_http import streamablehttp_client
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
from .identity import WORKLOAD_KEY, HeadersProvider, outbound

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
            client = MCPClient(
                lambda u=url, p=pool: streamablehttp_client(u, headers=p.headers())
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

def _build_session_manager(session_id: str, actor_id: str):
    """Build an AgentCoreMemorySessionManager for a specific session."""
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

    agentcore_config = AgentCoreMemoryConfig(
        memory_id=memory_id,
        session_id=session_id,
        actor_id=actor_id,
    )
    sm = AgentCoreMemorySessionManager(
        agentcore_memory_config=agentcore_config,
        region_name=region,
    )
    logger.info(f"AgentCore session manager created (memory={memory_id}, session={session_id}, actor={actor_id})")
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


def create_agent(session_id: Optional[str] = None, actor_id: str = "user") -> Agent:
    """Create a Strands agent for a given session.

    Args:
        session_id: Conversation session id. A new UUID is generated when None.
        actor_id: Identity of the caller (default "user").
    """
    session_id = session_id or str(uuid.uuid4())
    agent = _construct_agent(session_id, actor_id)
    logger.info(f"Agent created: {config.AGENT_NAME} session={session_id}")
    return agent


# ── session cache ────────────────────────────────────────────────────────

_agents: dict[tuple, Agent] = {}


def get_or_create_agent(session_id: Optional[str] = None, actor_id: str = "user") -> tuple[Agent, str]:
    """Return a cached agent for *session_id*, creating one if needed.

    Cached per (caller, session) rather than per session alone. `session_id`
    arrives from the request body as `contextId`, so keying on it alone would let
    one caller retrieve another caller's agent, whose MCP connections carry that
    caller's credential, by supplying a known context id.

    Returns (agent, session_id).
    """
    _, caller = outbound(config.PROPAGATE_CALLER_TOKEN)

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
