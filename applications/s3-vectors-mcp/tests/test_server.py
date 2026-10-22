"""Unit tests with stubbed AWS clients. No network, no credentials."""

from __future__ import annotations

import io
import json
import threading

import boto3
import pytest
from botocore.stub import ANY, Stubber

from app.config import Config, ConfigError
from app.embeddings import EmbeddingError, QueryEmbedder
from app.server import State, _explain, initialise
from app.vector_index import VectorSearch, describe

INDEX_RESP = {
    "index": {
        "vectorBucketName": "docs-vectors",
        "indexName": "docs",
        "indexArn": "arn:aws:s3vectors:us-west-2:111122223333:bucket/docs-vectors/index/docs",
        "creationTime": "2026-10-01T00:00:00Z",
        "dataType": "float32",
        "dimension": 4,
        "distanceMetric": "cosine",
        "metadataConfiguration": {"nonFilterableMetadataKeys": ["text"]},
    }
}


def cfg(**over) -> Config:
    base = dict(
        bucket="docs-vectors", index="docs", region="us-west-2", bedrock_region="us-west-2",
        embedding_model="amazon.titan-embed-text-v2:0", text_key="text", description="",
        default_top_k=3, max_top_k=5, component="docs-rag", host="127.0.0.1", port=8000,
    )
    base.update(over)
    return Config(**base)


class FakeBedrock:
    def __init__(self, vector, family="titan"):
        self.vector, self.family, self.calls = vector, family, []

    def invoke_model(self, **kw):
        self.calls.append(kw)
        payload = {"embeddings": [self.vector]} if self.family == "cohere" else {"embedding": self.vector}
        return {"body": io.BytesIO(json.dumps(payload).encode())}


@pytest.fixture
def s3v():
    client = boto3.client("s3vectors", region_name="us-west-2",
                          aws_access_key_id="x", aws_secret_access_key="x")
    with Stubber(client) as stub:
        yield client, stub


# ---- config ---------------------------------------------------------------

def test_config_requires_bucket_and_index(monkeypatch):
    monkeypatch.delenv("S3VECTORS_BUCKET", raising=False)
    monkeypatch.setenv("S3VECTORS_INDEX", "docs")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    with pytest.raises(ConfigError, match="S3VECTORS_BUCKET"):
        Config.from_env()


def test_config_defaults_loopback_and_titan_v2(monkeypatch):
    for k in ("HOST", "EMBEDDING_MODEL", "S3VECTORS_REGION"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("S3VECTORS_BUCKET", "b")
    monkeypatch.setenv("S3VECTORS_INDEX", "i")
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    c = Config.from_env()
    assert c.host == "127.0.0.1"
    assert c.embedding_model == "amazon.titan-embed-text-v2:0"
    assert c.region == "eu-west-1" and c.bedrock_region == "eu-west-1"


def test_bucket_region_override_keeps_bedrock_in_pod_region(monkeypatch):
    monkeypatch.setenv("S3VECTORS_BUCKET", "b")
    monkeypatch.setenv("S3VECTORS_INDEX", "i")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("S3VECTORS_REGION", "us-east-1")
    c = Config.from_env()
    assert (c.region, c.bedrock_region) == ("us-east-1", "us-west-2")


def test_config_rejects_default_above_max(monkeypatch):
    monkeypatch.setenv("S3VECTORS_BUCKET", "b")
    monkeypatch.setenv("S3VECTORS_INDEX", "i")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("DEFAULT_TOP_K", "50")
    monkeypatch.setenv("MAX_TOP_K", "10")
    with pytest.raises(ConfigError, match="exceeds"):
        Config.from_env()


# ---- embeddings -------------------------------------------------------------

def test_titan_v2_requests_index_dimension():
    fb = FakeBedrock([0.1] * 512)
    QueryEmbedder(fb, "amazon.titan-embed-text-v2:0", 512).embed("hello")
    assert json.loads(fb.calls[0]["body"]) == {"inputText": "hello", "dimensions": 512, "normalize": True}


def test_dimension_mismatch_is_refused():
    with pytest.raises(EmbeddingError, match="index has 384"):
        QueryEmbedder(FakeBedrock([]), "amazon.titan-embed-text-v2:0", 384)


def test_unsupported_model_is_refused():
    with pytest.raises(EmbeddingError, match="unsupported"):
        QueryEmbedder(FakeBedrock([]), "acme.embed-1", 1024)


def test_cohere_uses_search_query_input_type():
    fb = FakeBedrock([0.0] * 1024, family="cohere")
    QueryEmbedder(fb, "us.cohere.embed-english-v3", 1024).embed("q")
    assert json.loads(fb.calls[0]["body"]) == {"texts": ["q"], "input_type": "search_query"}


def test_wrong_returned_length_is_an_error():
    with pytest.raises(EmbeddingError, match="returned 3"):
        QueryEmbedder(FakeBedrock([0.1, 0.2, 0.3]), "amazon.titan-embed-text-v2:0", 256).embed("q")


# ---- search -------------------------------------------------------------------

def _search(client):
    from app.vector_index import IndexInfo
    c = cfg()
    emb = QueryEmbedder(FakeBedrock([0.1] * 256), c.embedding_model, 256)
    return VectorSearch(client, emb, c, IndexInfo("docs-vectors", "docs", 256, "cosine", "float32"))


def test_search_sends_read_only_query_and_splits_text(s3v):
    client, stub = s3v
    stub.add_response(
        "query_vectors",
        {"vectors": [{"key": "guide#3", "distance": 0.12,
                      "metadata": {"text": "Refunds take 5 days.", "source": "guide.pdf", "page": 3}}],
         "distanceMetric": "cosine"},
        {"vectorBucketName": "docs-vectors", "indexName": "docs",
         "queryVector": {"float32": ANY}, "topK": 3, "returnMetadata": True, "returnDistance": True},
    )
    out = _search(client).search("refund policy?", None, None)
    assert out["results"] == [{"key": "guide#3", "distance": 0.12, "text": "Refunds take 5 days.",
                               "metadata": {"source": "guide.pdf", "page": 3}}]


def test_search_passes_filter(s3v):
    client, stub = s3v
    f = {"source": {"$eq": "guide.pdf"}}
    stub.add_response("query_vectors", {"vectors": [], "distanceMetric": "cosine"},
                      {"vectorBucketName": ANY, "indexName": ANY, "queryVector": ANY, "topK": 2,
                       "returnMetadata": True, "returnDistance": True, "filter": f})
    assert _search(client).search("q", 2, f)["results"] == []


@pytest.mark.parametrize("top_k", [0, 6])
def test_top_k_bounds(s3v, top_k):
    client, _ = s3v
    with pytest.raises(ValueError, match="top_k"):
        _search(client).search("q", top_k, None)


def test_empty_query_rejected(s3v):
    client, _ = s3v
    with pytest.raises(ValueError, match="empty"):
        _search(client).search("  ", None, None)


# ---- readiness ----------------------------------------------------------------

def test_describe_reads_dimension_and_keys(s3v):
    client, stub = s3v
    stub.add_response("get_index", INDEX_RESP, {"vectorBucketName": "docs-vectors", "indexName": "docs"})
    info = describe(client, cfg())
    assert (info.dimension, info.distance_metric, info.non_filterable_keys) == (4, "cosine", ["text"])


def test_initialise_reports_access_denied_with_fix_then_recovers(s3v, monkeypatch):
    client, stub = s3v
    stub.add_client_error("get_index", service_error_code="AccessDeniedException", http_status_code=403)
    resp = json.loads(json.dumps(INDEX_RESP))
    resp["index"]["dimension"] = 256
    stub.add_response("get_index", resp)
    state, stop, seen = State(), threading.Event(), []
    orig = State.set

    def spy(self, search, error):
        seen.append(error)
        orig(self, search, error)

    monkeypatch.setattr(State, "set", spy)
    monkeypatch.setattr(threading.Event, "wait", lambda self, t=None: False)
    initialise(cfg(), state, client, FakeBedrock([0.0] * 256), stop)
    assert "accessFor: [docs-rag]" in seen[0]
    assert state.search is not None and state.error == ""


def test_not_found_message_names_bucket_and_index():
    from botocore.exceptions import ClientError
    e = ClientError({"Error": {"Code": "NotFoundException", "Message": "x"}}, "GetIndex")
    assert _explain(cfg(), e) == "index 'docs' not found in vector bucket 'docs-vectors' (us-west-2)"


def test_no_credentials_points_at_the_trait():
    from botocore.exceptions import NoCredentialsError
    assert "aws-service-identity trait with accessFor: [docs-rag]" in _explain(cfg(), NoCredentialsError())


def test_tools_are_read_only():
    from app.server import build
    import asyncio
    names = sorted(t.name for t in asyncio.run(build(cfg(), State()).list_tools()))
    assert names == ["describe_index", "search"]
