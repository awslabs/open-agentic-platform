"""The one S3 Vectors index this server answers from.

Read-only by construction: the only S3 Vectors calls in this module are GetIndex and
QueryVectors. QueryVectors with returnMetadata or a filter also needs
s3vectors:GetVectors (QueryVectors API reference), which the component's IAM policy
grants on this index only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import Config
from .embeddings import QueryEmbedder


@dataclass(frozen=True)
class IndexInfo:
    bucket: str
    index: str
    dimension: int
    distance_metric: str
    data_type: str
    non_filterable_keys: list[str] = field(default_factory=list)


def describe(s3vectors: Any, cfg: Config) -> IndexInfo:
    idx = s3vectors.get_index(vectorBucketName=cfg.bucket, indexName=cfg.index)["index"]
    meta = idx.get("metadataConfiguration") or {}
    return IndexInfo(
        bucket=cfg.bucket,
        index=cfg.index,
        dimension=int(idx["dimension"]),
        distance_metric=str(idx.get("distanceMetric", "")),
        data_type=str(idx.get("dataType", "")),
        non_filterable_keys=list(meta.get("nonFilterableMetadataKeys") or []),
    )


class VectorSearch:
    def __init__(self, s3vectors: Any, embedder: QueryEmbedder, cfg: Config, info: IndexInfo):
        self._s3v = s3vectors
        self._embedder = embedder
        self._cfg = cfg
        self.info = info

    def search(self, query: str, top_k: int | None, metadata_filter: dict | None) -> dict[str, Any]:
        query = (query or "").strip()
        if not query:
            raise ValueError("query must not be empty")
        k = self._cfg.default_top_k if top_k is None else int(top_k)
        if k < 1 or k > self._cfg.max_top_k:
            raise ValueError(f"top_k must be between 1 and {self._cfg.max_top_k}")
        if metadata_filter is not None and not isinstance(metadata_filter, dict):
            raise ValueError("filter must be an object, e.g. {\"source\": {\"$eq\": \"guide.pdf\"}}")

        request: dict[str, Any] = {
            "vectorBucketName": self._cfg.bucket,
            "indexName": self._cfg.index,
            "queryVector": {"float32": self._embedder.embed(query)},
            "topK": k,
            "returnMetadata": True,
            "returnDistance": True,
        }
        if metadata_filter:
            request["filter"] = metadata_filter
        resp = self._s3v.query_vectors(**request)

        results = []
        for v in resp.get("vectors", []):
            metadata = dict(v.get("metadata") or {})
            text = metadata.pop(self._cfg.text_key, None)
            results.append(
                {
                    "key": v.get("key"),
                    "distance": v.get("distance"),
                    "text": text,
                    "metadata": metadata,
                }
            )
        return {
            "index": self._cfg.index,
            "distanceMetric": resp.get("distanceMetric", self.info.distance_metric),
            "results": results,
        }
