"""Read-only semantic search over one Amazon S3 Vectors index, as an MCP server.

Liveness (/healthz) is up as soon as the process serves HTTP. Readiness (/readyz) is
503 until the index has been described and the embedding model checked against its
dimension. That separation matters on first deploy: the IAM policy attached by
aws-service-identity can take a minute to propagate, and a liveness probe that
depended on AWS would restart the pod through it.
"""

import logging
import threading
from typing import Annotated, Any

import anyio
import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

from .config import Config
from .embeddings import EmbeddingError, QueryEmbedder
from .vector_index import VectorSearch, describe

log = logging.getLogger("s3-vectors-mcp")

_BOTO = BotoConfig(retries={"max_attempts": 3, "mode": "standard"}, connect_timeout=5, read_timeout=30)


class State:
    """Initialisation result, shared between the readiness loop and the tools."""

    def __init__(self) -> None:
        self.search: VectorSearch | None = None
        self.error: str = "starting"
        self._lock = threading.Lock()

    def set(self, search: VectorSearch | None, error: str) -> None:
        with self._lock:
            self.search, self.error = search, error

    def ready(self) -> VectorSearch:
        s = self.search
        if s is None:
            # ToolError: its text reaches the agent; any other exception is hidden.
            raise ToolError(f"vector index not ready: {self.error}")
        return s


def _explain(cfg: Config, e: Exception) -> str:
    name = cfg.component or "<this component>"
    if isinstance(e, NoCredentialsError):
        return (
            f"no AWS credentials. Add the aws-service-identity trait with accessFor: [{name}] "
            f"to this component"
        )
    if isinstance(e, ClientError):
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("AccessDeniedException", "AccessDenied"):
            return (
                f"{code} on bucket {cfg.bucket!r} index {cfg.index!r}. Check that the component "
                f"has the aws-service-identity trait with accessFor: [{name}] (its own name), "
                f"and allow a minute for a new IAM policy to propagate"
            )
        if code == "NotFoundException":
            return f"index {cfg.index!r} not found in vector bucket {cfg.bucket!r} ({cfg.region})"
        return f"{code}: {e.response.get('Error', {}).get('Message', '')}"
    return f"{type(e).__name__}: {e}"


def initialise(cfg: Config, state: State, s3vectors: Any, bedrock: Any, stop: threading.Event) -> None:
    """Describe the index and build the embedder, retrying until it works.

    Retries forever with a capped backoff: every failure here (IAM propagation, an
    index the pipeline has not created yet, a model mismatch fixed by redeploying the
    pipeline) can resolve without restarting this pod.
    """
    delay = 2.0
    while not stop.is_set():
        try:
            info = describe(s3vectors, cfg)
            embedder = QueryEmbedder(bedrock, cfg.embedding_model, info.dimension)
            state.set(VectorSearch(s3vectors, embedder, cfg, info), "")
            log.info(
                "ready: bucket=%s index=%s dimension=%d metric=%s model=%s",
                cfg.bucket, cfg.index, info.dimension, info.distance_metric, cfg.embedding_model,
            )
            return
        except (ClientError, BotoCoreError, EmbeddingError, KeyError, ValueError) as e:
            msg = _explain(cfg, e)
            state.set(None, msg)
            log.warning("not ready: %s (retrying in %.0fs)", msg, delay)
        stop.wait(delay)
        delay = min(delay * 2, 60.0)


def build(cfg: Config, state: State) -> MCPServer:
    about = cfg.description or f"documents in S3 Vectors index {cfg.index!r}"
    mcp = MCPServer(
        name=cfg.component or "s3-vectors-mcp",
        instructions=(
            f"Semantic search over {about}. Call `search` with a natural-language question "
            f"and cite the returned `metadata` (for example source and page) in answers."
        ),
    )

    @mcp.tool(
        description=(
            f"Semantic search over {about}. Returns the most similar text chunks with their "
            f"metadata and distance (lower is closer). Optional `filter` uses S3 Vectors "
            f"metadata filter syntax, e.g. {{\"source\": {{\"$eq\": \"guide.pdf\"}}}}."
        )
    )
    async def search(
        query: Annotated[str, Field(description="Natural-language question or keywords")],
        top_k: Annotated[
            int | None,
            Field(description=f"Results to return, 1-{cfg.max_top_k} (default {cfg.default_top_k})"),
        ] = None,
        filter: Annotated[
            dict[str, Any] | None,
            Field(description="Optional S3 Vectors metadata filter on filterable keys"),
        ] = None,
    ) -> dict[str, Any]:
        s = state.ready()
        try:
            return await anyio.to_thread.run_sync(s.search, query, top_k, filter)
        except ValueError as e:
            raise ToolError(str(e)) from e
        except (ClientError, BotoCoreError) as e:
            raise ToolError(_explain(cfg, e)) from e

    @mcp.tool(description="Describe the index: dimension, distance metric, and which metadata keys can be filtered.")
    async def describe_index() -> dict[str, Any]:
        info = state.ready().info
        return {
            "bucket": info.bucket,
            "index": info.index,
            "description": cfg.description,
            "dimension": info.dimension,
            "distanceMetric": info.distance_metric,
            "dataType": info.data_type,
            "embeddingModel": cfg.embedding_model,
            "textMetadataKey": cfg.text_key,
            "nonFilterableMetadataKeys": info.non_filterable_keys,
        }

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    @mcp.custom_route("/readyz", methods=["GET"])
    async def readyz(_: Request) -> JSONResponse:
        if state.search is None:
            return JSONResponse({"ready": False, "reason": state.error}, status_code=503)
        return JSONResponse({"ready": True})

    return mcp


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = Config.from_env()
    state = State()
    s3vectors = boto3.client("s3vectors", region_name=cfg.region, config=_BOTO)
    # Bedrock in the pod's own region; the vector bucket may be elsewhere.
    bedrock = boto3.client("bedrock-runtime", region_name=cfg.bedrock_region, config=_BOTO)
    stop = threading.Event()
    threading.Thread(
        target=initialise, args=(cfg, state, s3vectors, bedrock, stop), daemon=True, name="init"
    ).start()
    log.info("serving MCP on %s:%d/mcp (stateless)", cfg.host, cfg.port)
    try:
        # Stateless: each request carries everything it needs and the server keeps no
        # per-session state, so any replica can answer any request.
        build(cfg, state).run(
            transport="streamable-http",
            host=cfg.host,
            port=cfg.port,
            stateless_http=True,
            json_response=True,
        )
    finally:
        stop.set()


if __name__ == "__main__":
    main()
