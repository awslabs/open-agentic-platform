"""Configuration, read once from the environment.

The s3-vectors-mcp OAM component sets every value below from its typed parameters,
so a developer never writes these env vars by hand.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigError(ValueError):
    """A required setting is missing or invalid. The server refuses to start."""


def _int(name: str, default: int, minimum: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as e:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from e
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


@dataclass(frozen=True)
class Config:
    bucket: str
    index: str
    region: str
    bedrock_region: str
    embedding_model: str
    text_key: str
    description: str
    default_top_k: int
    max_top_k: int
    component: str
    host: str
    port: int

    @classmethod
    def from_env(cls) -> "Config":
        bucket = os.environ.get("S3VECTORS_BUCKET", "").strip()
        index = os.environ.get("S3VECTORS_INDEX", "").strip()
        if not bucket:
            raise ConfigError("S3VECTORS_BUCKET is required")
        if not index:
            raise ConfigError("S3VECTORS_INDEX is required")
        # The vector bucket may live in another region than the cluster; Bedrock is
        # called in the pod's own region (AWS_REGION, injected by aws-service-identity).
        # The pod's own region, injected by aws-service-identity as both names.
        pod_region = (
            os.environ.get("AWS_REGION", "").strip()
            or os.environ.get("AWS_DEFAULT_REGION", "").strip()
        )
        region = os.environ.get("S3VECTORS_REGION", "").strip() or pod_region
        if not region:
            raise ConfigError("S3VECTORS_REGION or AWS_REGION is required")
        default_top_k = _int("DEFAULT_TOP_K", 5, 1)
        max_top_k = _int("MAX_TOP_K", 20, 1)
        if default_top_k > max_top_k:
            raise ConfigError(f"DEFAULT_TOP_K ({default_top_k}) exceeds MAX_TOP_K ({max_top_k})")
        return cls(
            bucket=bucket,
            index=index,
            region=region,
            bedrock_region=pod_region or region,
            embedding_model=os.environ.get("EMBEDDING_MODEL", "").strip()
            or "amazon.titan-embed-text-v2:0",
            text_key=os.environ.get("TEXT_METADATA_KEY", "").strip() or "text",
            description=os.environ.get("INDEX_DESCRIPTION", "").strip(),
            default_top_k=default_top_k,
            max_top_k=max_top_k,
            component=os.environ.get("COMPONENT_NAME", "").strip(),
            # Loopback by default. The OAM component sets HOST=0.0.0.0 so the pod's
            # ClusterIP Service can reach it; nothing publishes the port beyond that.
            host=os.environ.get("HOST", "").strip() or "127.0.0.1",
            port=_int("PORT", 8000, 1),
        )
