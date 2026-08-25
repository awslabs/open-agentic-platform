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

logger = logging.getLogger(__name__)

# ── shared resources (created once) ──────────────────────────────────────

_model: Optional[OpenAIModel] = None
_mcp_tools: list = []
_mcp_clients: list = []
_mcp_connected_at: Optional[float] = None

# The gateway-identity token (audience "agentgateway") mounted at
# WORKLOAD_TOKEN_PATH has a fixed TTL (expirationSeconds on the projected
# ServiceAccount token, currently 1h). _get_mcp_tools() opens a persistent
# MCP connection per server and reuses it, so a long-lived agent session
# eventually calls a tool with the credentials the connection authenticated
# with at connect time — which expire even though the token *file* on disk
# gets rotated by the kubelet, because the open connection doesn't re-read
# it. Recycle each MCPClient in place (stop() + start() on the same
# instance, which re-invokes the transport callable and therefore
# _gateway_headers()) after this many seconds, well under the token's 1h
# lifetime. Reconnecting the same instances (rather than creating new ones)
# keeps any already-built Agent's cached tool objects valid, since those
# tools are bound to the MCPClient object identity, not a point-in-time
# session.
_MCP_CONNECTION_MAX_AGE_SECONDS = 45 * 60


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


def _gateway_headers() -> dict:
    """Authorization header from the projected workload-identity token.

    The `gateway-identity` OAM trait mounts a projected ServiceAccount token
    (audience `agentgateway`) and sets WORKLOAD_TOKEN_PATH. AgentGateway
    validates it against the cluster OIDC issuer. Read fresh on each connect so
    the kubelet-rotated token is always current. Returns {} when no token is
    mounted (gateway auth not in use).
    """
    path = os.getenv("WORKLOAD_TOKEN_PATH")
    if path:
        try:
            with open(path) as f:
                return {"Authorization": "Bearer " + f.read().strip()}
        except OSError:
            logger.warning("WORKLOAD_TOKEN_PATH set but token unreadable at %s", path)
    return {}


def _get_mcp_tools() -> list:
    global _mcp_tools, _mcp_clients, _mcp_connected_at

    if _mcp_clients:
        age = time.monotonic() - _mcp_connected_at
        if age < _MCP_CONNECTION_MAX_AGE_SECONDS:
            return _mcp_tools
        logger.info(
            "Recycling %d MCP connection(s) after %.0fs (max age %ds) so the "
            "gateway auth token is re-read fresh",
            len(_mcp_clients), age, _MCP_CONNECTION_MAX_AGE_SECONDS,
        )
        for client in _mcp_clients:
            try:
                client.stop(None, None, None)
                client.start()
            except Exception as exc:
                logger.warning(f"  Failed to recycle MCP connection: {exc}")
        _mcp_connected_at = time.monotonic()
        return _mcp_tools

    urls = config.MCP_SERVER_URLS
    if not urls:
        return []

    clients: list = []
    tools: list = []
    for url in urls:
        logger.info(f"Connecting to MCP server: {url}")
        try:
            client = MCPClient(lambda u=url: streamablehttp_client(u, headers=_gateway_headers()))
            client.start()
            server_tools = client.list_tools_sync()
            logger.info(f"  Loaded {len(server_tools)} tools from {url}")
            clients.append(client)
            tools.extend(server_tools)
        except Exception as exc:
            logger.warning(f"  Failed to connect to MCP server {url}: {exc}")

    _mcp_tools = tools
    _mcp_clients = clients
    _mcp_connected_at = time.monotonic()
    return _mcp_tools


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
    tools = _get_mcp_tools() or None
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

_agents: dict[str, Agent] = {}


def get_or_create_agent(session_id: Optional[str] = None, actor_id: str = "user") -> tuple[Agent, str]:
    """Return a cached agent for *session_id*, creating one if needed.

    Returns (agent, session_id).
    """
    if session_id and session_id in _agents:
        return _agents[session_id], session_id

    sid = session_id or str(uuid.uuid4())
    agent = create_agent(session_id=sid, actor_id=actor_id)
    _agents[sid] = agent
    return agent, sid


# ── cleanup ──────────────────────────────────────────────────────────────

def shutdown_mcp() -> None:
    global _mcp_clients
    if _mcp_clients:
        logger.info("Closing MCP client connections")
        for client in _mcp_clients:
            try:
                client.stop(None, None, None)
            except Exception as exc:
                logger.warning(f"  Failed to close MCP connection: {exc}")
        _mcp_clients = []
