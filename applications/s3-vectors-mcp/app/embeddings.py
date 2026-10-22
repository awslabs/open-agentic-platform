"""Query embedding with Amazon Bedrock.

Only the QUERY is embedded here. Documents are embedded by the ingestion pipeline,
which must use the same model and dimension, or search results are meaningless.
The server checks the dimension against the index at startup.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


class EmbeddingError(ValueError):
    """The configured model cannot produce vectors for this index."""


@dataclass(frozen=True)
class _Family:
    name: str
    # Output sizes the model can produce. Titan v2 takes a requested size; the
    # others have one fixed size.
    dimensions: tuple[int, ...]


# Matched by substring so a cross-region inference profile id ("us.cohere...")
# resolves to the same family as the plain model id.
_FAMILIES: dict[str, _Family] = {
    "amazon.titan-embed-text-v2": _Family("titan-v2", (256, 512, 1024)),
    "amazon.titan-embed-text-v1": _Family("titan-v1", (1536,)),
    "cohere.embed-english-v3": _Family("cohere-v3", (1024,)),
    "cohere.embed-multilingual-v3": _Family("cohere-v3", (1024,)),
}


def _family(model_id: str) -> _Family:
    for prefix, family in _FAMILIES.items():
        if prefix in model_id:
            return family
    raise EmbeddingError(
        f"unsupported embedding model {model_id!r}; supported: {', '.join(sorted(_FAMILIES))}"
    )


class QueryEmbedder:
    def __init__(self, bedrock_runtime: Any, model_id: str, dimension: int):
        family = _family(model_id)
        if dimension not in family.dimensions:
            raise EmbeddingError(
                f"model {model_id!r} produces {list(family.dimensions)} dimensions but the "
                f"index has {dimension}; the index must be built with the same model the "
                f"server queries with"
            )
        self._client = bedrock_runtime
        self._model_id = model_id
        self._family = family
        self._dimension = dimension

    def _body(self, text: str) -> dict[str, Any]:
        if self._family.name == "titan-v2":
            return {"inputText": text, "dimensions": self._dimension, "normalize": True}
        if self._family.name == "titan-v1":
            return {"inputText": text}
        return {"texts": [text], "input_type": "search_query"}

    def embed(self, text: str) -> list[float]:
        resp = self._client.invoke_model(
            modelId=self._model_id,
            body=json.dumps(self._body(text)),
            contentType="application/json",
            accept="application/json",
        )
        payload = json.loads(resp["body"].read())
        vector = payload["embeddings"][0] if self._family.name == "cohere-v3" else payload["embedding"]
        if len(vector) != self._dimension:
            raise EmbeddingError(
                f"model returned {len(vector)} dimensions, index expects {self._dimension}"
            )
        return [float(v) for v in vector]
