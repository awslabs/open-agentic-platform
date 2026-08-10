"""FastAPI application with A2A protocol support and per-session agents."""

import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict

import uvicorn
from strands.multiagent.a2a import A2AServer

from .agent import create_agent, get_or_create_agent, shutdown_mcp
from .config import config

# ── OpenTelemetry initialization ─────────────────────────────────────────
# Three modes (mutually exclusive, checked in order):
# 1. Decentralized (OTEL_PYTHON_DISTRO=aws_distro) — ADOT handles everything,
#    agent exports directly to CloudWatch. No manual init needed.
# 2. Direct to Langfuse (LANGFUSE_BASE_URL set) — agent sends OTLP directly
# 3. Via Collector (OTEL_EXPORTER_OTLP_ENDPOINT set) — agent sends to local collector
if os.getenv("OTEL_PYTHON_DISTRO") == "aws_distro":
    # Decentralized mode: ADOT auto-instrumentation handles telemetry.
    pass
elif os.getenv("LANGFUSE_BASE_URL"):
    try:
        import base64
        from strands.telemetry import StrandsTelemetry

        auth_str = f"{os.getenv('LANGFUSE_PUBLIC_KEY', '')}:{os.getenv('LANGFUSE_SECRET_KEY', '')}"
        auth_bytes = base64.b64encode(auth_str.encode()).decode()

        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = os.getenv("LANGFUSE_BASE_URL") + "/api/public/otel"
        os.environ["OTEL_EXPORTER_OTLP_HEADERS"] = f"Authorization=Basic {auth_bytes},x-langfuse-ingestion-version=4"

        strands_telemetry = StrandsTelemetry().setup_otlp_exporter()
    except ImportError:
        pass
elif os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
    try:
        from strands.telemetry import StrandsTelemetry
        strands_telemetry = StrandsTelemetry().setup_otlp_exporter()
    except ImportError:
        pass

# ── HTTP client instrumentation (W3C traceparent propagation) ────────────
# Instruments httpx so outbound calls to Bifrost carry the traceparent header.
# Bifrost reads traceparent and creates child spans under the same trace ID,
# producing a unified trace tree: Agent → Bifrost LLM call.
try:
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    HTTPXClientInstrumentor().instrument()
except ImportError:
    pass

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL.upper()),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app):
    yield
    shutdown_mcp()


# A2AServer builds one Agent per A2A context via agent_factory (context_id ->
# Agent), so each caller/session gets its own AgentCore-backed session_manager
# instead of all A2A callers sharing a single memory-less agent. create_agent's
# signature (session_id, actor_id="user") matches (context_id) -> Agent when
# called positionally. The factory is invoked once up front (with a placeholder
# context id) purely to derive agent-card metadata.
a2a_server = A2AServer(
    agent_factory=create_agent,
    host=config.HOST,
    port=config.PORT,
    version="1.0.0",
    enable_a2a_compliant_streaming=True,
)

app = a2a_server.to_fastapi_app()
app.router.lifespan_context = lifespan


@app.get("/health")
@app.get("/ping")
async def health() -> Dict[str, str]:
    return {
        "status": "healthy",
        "agent": config.AGENT_NAME,
        "a2a_protocol": "compatible",
    }


@app.post("/chat")
async def simple_chat(request: Dict[str, Any]) -> Dict[str, Any]:
    """Chat endpoint with per-session agent and AgentCore memory.

    Request:
        { "message": "...", "contextId": "optional-session-id" }
    Response:
        { "response": "...", "contextId": "session-id" }
    """
    user_message = request.get("message", "")
    context_id = request.get("contextId")

    try:
        agent, session_id = get_or_create_agent(session_id=context_id)
        result = await agent.invoke_async(user_message)

        if isinstance(result, dict):
            response_text = result.get("response", str(result))
        elif isinstance(result, str):
            response_text = result
        else:
            response_text = str(result)

        return {"response": response_text, "contextId": session_id}
    except Exception as e:
        logger.error(f"Error in /chat: {e}")
        return {"error": str(e), "contextId": context_id or "error"}


def main():
    logger.info(f"Starting {config.AGENT_NAME} on {config.HOST}:{config.PORT}")
    logger.info("=" * 60)
    logger.info("A2A Protocol Endpoints (JSON-RPC at root):")
    logger.info("  - Agent Card: GET /.well-known/agent.json")
    logger.info("  - Send Message: POST / (JSON-RPC 2.0)")
    logger.info("Custom Endpoints:")
    logger.info("  - Simple Chat: POST /chat (per-session agent)")
    logger.info("  - Health: GET /health")
    logger.info("=" * 60)
    logger.info(f"Model: {config.MODEL_ID}")
    logger.info(f"LLM Gateway: {config.LLM_GATEWAY_URL}")
    logger.info(f"Memory: {config.MEMORY_PROVIDER or 'none'}")
    logger.info("=" * 60)
    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level=config.LOG_LEVEL.lower())


if __name__ == "__main__":
    main()
