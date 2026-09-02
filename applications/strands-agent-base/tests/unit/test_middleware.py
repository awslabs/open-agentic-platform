"""Middleware tests: the caller's header must reach agent construction.

Exercises the real mechanism (a ContextVar set in HTTP middleware, read later on
the request's task) against a minimal app, rather than trusting that it works.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.identity import WORKLOAD_KEY, capture_caller_auth, outbound


@pytest.fixture
def client(tmp_path, monkeypatch):
    sa = tmp_path / "token"
    sa.write_text("sa-token-v1")
    monkeypatch.setenv("WORKLOAD_TOKEN_PATH", str(sa))

    app = FastAPI()
    app.middleware("http")(capture_caller_auth)

    @app.get("/resolved")
    def resolved():
        headers, key = outbound()
        return {"authorization": headers().get("Authorization"), "key": key}

    return TestClient(app)


def test_caller_header_reaches_the_handler(client):
    body = client.get("/resolved", headers={"Authorization": "Bearer alice.jwt"}).json()
    assert body["authorization"] == "Bearer alice.jwt"
    assert body["key"] != WORKLOAD_KEY


def test_request_without_header_falls_back_to_service_account(client):
    body = client.get("/resolved").json()
    assert body["authorization"] == "Bearer sa-token-v1"
    assert body["key"] == WORKLOAD_KEY


def test_header_does_not_leak_into_a_later_request(client):
    client.get("/resolved", headers={"Authorization": "Bearer alice.jwt"})
    body = client.get("/resolved").json()
    assert body["authorization"] == "Bearer sa-token-v1", "credential leaked across requests"
    assert body["key"] == WORKLOAD_KEY


def test_each_caller_is_resolved_independently(client):
    alice = client.get("/resolved", headers={"Authorization": "Bearer alice.jwt"}).json()
    bob = client.get("/resolved", headers={"Authorization": "Bearer bob.jwt"}).json()
    assert alice["key"] != bob["key"]
    assert bob["authorization"] == "Bearer bob.jwt"
