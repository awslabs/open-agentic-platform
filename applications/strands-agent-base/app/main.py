"""FastAPI application with A2A protocol support and per-session agents."""

import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict

import uvicorn
from fastapi.responses import JSONResponse
from strands.multiagent.a2a import A2AServer

from .agent import create_agent, get_or_create_agent, mcp_readiness, shutdown_mcp
from .config import config
from .identity import capture_caller_auth

# ── OpenTelemetry initialization ─────────────────────────────────────────
# A tracer provider whose sampler drops high-frequency a2a-sdk event-queue
# plumbing spans while keeping everything useful (request-handler root span,
# agent, LLM generations, tool calls). The a2a-sdk traces every method of its
# EventQueue/EventConsumer/QueueManager classes (@trace_class), emitting tens of
# thousands of enqueue/dequeue/task_done spans per turn under names like
# "a2a.server.events.event_queue.EventQueueLegacy.dequeue_event" — they bury the
# real tree in Langfuse. Their span names all share the module prefix
# "a2a.server.events." (span name = "<module>.<Class>.<method>"), whereas the
# useful root span lives under "a2a.server.request_handlers.", so a name-prefix
# drop is surgical and version-robust. The a2a-sdk also @trace-decorates its
# module-level helpers under "a2a.utils." (e.g.
# "a2a.utils.helpers.append_artifact_to_task"), emitted once per artifact/event
# — pure plumbing that likewise buries the tree — so that prefix is dropped too.
# Overridable via OTEL_DROP_SPAN_NAME_PREFIXES (comma-separated); empty disables
# filtering.
#
# We can't set this via OTEL_TRACES_SAMPLER (no built-in name filter) so we
# build the provider and hand it to StrandsTelemetry(tracer_provider=...).
# StrandsTelemetry only globalizes + sets propagators when it creates the
# provider itself, so when we pass our own we must replicate both here.
def _build_filtered_tracer_provider():
    """SDK TracerProvider that drops noisy a2a event-queue spans.

    Returns the provider (already set as global, with W3C propagators) or None
    if the OTEL SDK isn't importable, so callers fall back to the default
    StrandsTelemetry provider.
    """
    try:
        from opentelemetry import propagate as _propagate
        from opentelemetry import trace as _trace_api
        from opentelemetry.baggage.propagation import W3CBaggagePropagator
        from opentelemetry.propagators.composite import CompositePropagator
        from opentelemetry.sdk.trace import TracerProvider as SDKTracerProvider
        from opentelemetry.sdk.trace.sampling import (
            ALWAYS_ON,
            Decision,
            Sampler,
            SamplingResult,
        )
        from opentelemetry.trace import get_current_span
        from opentelemetry.trace.propagation.tracecontext import (
            TraceContextTextMapPropagator,
        )
        from strands.telemetry.config import get_otel_resource
    except Exception:
        return None

    prefixes = tuple(
        p.strip()
        for p in os.getenv(
            "OTEL_DROP_SPAN_NAME_PREFIXES", "a2a.server.events.,a2a.utils."
        ).split(",")
        if p.strip()
    )
    if not prefixes:
        return None

    class _DropNoisySpans(Sampler):
        # Delegate to ALWAYS_ON (not ParentBased) for kept spans so a useful
        # span is never dropped as a side effect of its parent being dropped:
        # dropped event-queue spans thus don't cascade to any child that isn't
        # itself event-queue.
        def should_sample(
            self, parent_context, trace_id, name, kind=None,
            attributes=None, links=None, trace_state=None,
        ):
            if name.startswith(prefixes):
                ts = get_current_span(parent_context).get_span_context().trace_state
                return SamplingResult(Decision.DROP, None, ts)
            return ALWAYS_ON.should_sample(
                parent_context, trace_id, name, kind, attributes, links, trace_state
            )

        def get_description(self):
            return f"DropNoisySpans(prefixes={prefixes})"

    provider = SDKTracerProvider(resource=get_otel_resource(), sampler=_DropNoisySpans())
    _trace_api.set_tracer_provider(provider)
    _propagate.set_global_textmap(
        CompositePropagator([W3CBaggagePropagator(), TraceContextTextMapPropagator()])
    )
    return provider


def _setup_otlp_telemetry():
    """Configure the OTLP exporter on a noise-filtered provider when possible,
    else fall back to StrandsTelemetry's default provider."""
    from strands.telemetry import StrandsTelemetry

    provider = _build_filtered_tracer_provider()
    if provider is not None:
        StrandsTelemetry(tracer_provider=provider).setup_otlp_exporter()
    else:
        StrandsTelemetry().setup_otlp_exporter()


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

        auth_str = f"{os.getenv('LANGFUSE_PUBLIC_KEY', '')}:{os.getenv('LANGFUSE_SECRET_KEY', '')}"
        auth_bytes = base64.b64encode(auth_str.encode()).decode()

        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = os.getenv("LANGFUSE_BASE_URL") + "/api/public/otel"
        os.environ["OTEL_EXPORTER_OTLP_HEADERS"] = f"Authorization=Basic {auth_bytes},x-langfuse-ingestion-version=4"

        _setup_otlp_telemetry()
    except ImportError:
        pass
elif os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
    try:
        _setup_otlp_telemetry()
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

# Record the caller's Authorization header so agent construction can forward it
# to MCP instead of the agent's own ServiceAccount token. Covers every route,
# including the SDK-owned A2A JSON-RPC endpoint. See app/identity.py.
app.middleware("http")(capture_caller_auth)


@app.get("/health")
@app.get("/ping")
async def health() -> Dict[str, str]:
    return {
        "status": "healthy",
        "agent": config.AGENT_NAME,
        "a2a_protocol": "compatible",
    }


# Deliberately a plain `def`, not `async def`: mcp_readiness opens MCP
# connections, which blocks. FastAPI runs sync endpoints in a worker thread, so
# the blocking connect never stalls the asyncio event loop (an `async def` here
# would). The Kubernetes readiness probe calls this every periodSeconds; while
# any configured MCP server is not connected it retries the missing ones (one
# attempt per probe) and returns 503, so the pod is kept out of the Service
# until its toolset is complete. /health stays always-200 for liveness, so a
# transiently-not-ready pod is not restarted — only kept un-Ready.
@app.get("/ready")
def ready():
    ok, reasons = mcp_readiness()
    if ok:
        return {"status": "ready", "agent": config.AGENT_NAME}
    return JSONResponse(
        status_code=503,
        content={"status": "not-ready", "agent": config.AGENT_NAME, "missing": reasons},
    )


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
