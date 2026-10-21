"""Shared fixtures for the live scenario matrices.

Each matrix row is one test whose id is the row id (I1, P2, ...), so a run reads like the
matrix in the design docs and one failing row never hides the others.

Environment:
  KEYCLOAK_HOST     host serving /keycloak/realms/platform (required)
  SPOKE_KUBECONFIG  cluster running the gateway and the agents [$KUBECONFIG]
  HUB_KUBECONFIG    cluster holding the keycloak/keycloak-config secret [SPOKE_KUBECONFIG]

Requests reach the gateway through `kubectl port-forward` to its Service, so no public
URL is needed. The gateway still validates every token exactly as for in-cluster calls.
"""

import base64
import json
import os
import socket
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from kubernetes import client as k8s
from kubernetes import config as k8s_config

REPO = Path(__file__).resolve().parents[2]
GATEWAY_NS, GATEWAY_SVC = "agentgateway-system", "agentgateway-proxy"
AUDIENCE = "agentgateway"


def _env(name, default=None):
    value = os.getenv(name, default)
    if not value:
        pytest.exit(f"set {name}", returncode=2)
    return value


@pytest.fixture(scope="session")
def spoke_kubeconfig():
    return _env("SPOKE_KUBECONFIG", os.getenv("KUBECONFIG"))


@pytest.fixture(scope="session")
def core(spoke_kubeconfig):
    return k8s.CoreV1Api(k8s_config.new_client_from_config(config_file=spoke_kubeconfig))


@pytest.fixture(scope="session")
def custom(spoke_kubeconfig):
    return k8s.CustomObjectsApi(k8s_config.new_client_from_config(config_file=spoke_kubeconfig))


@pytest.fixture(scope="session")
def kubectl(spoke_kubeconfig):
    """Run kubectl against the spoke; returns CompletedProcess (never raises)."""

    def run(*args, timeout=120):
        return subprocess.run(
            ["kubectl", "--kubeconfig", spoke_kubeconfig, "--request-timeout=30s", *args],
            capture_output=True, text=True, timeout=timeout,
        )

    return run


@pytest.fixture(scope="session")
def started():
    """Start of the run; log reads are limited to lines written after it."""
    return datetime.now(timezone.utc)


@pytest.fixture(scope="session")
def gateway(spoke_kubeconfig):
    """Base URL of the gateway through a port-forward held for the session."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen(
        ["kubectl", "--kubeconfig", spoke_kubeconfig, "port-forward", "-n", GATEWAY_NS,
         f"svc/{GATEWAY_SVC}", f"{port}:8080"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            httpx.get(url, timeout=2)
            break
        except httpx.TransportError:
            if proc.poll() is not None:
                pytest.exit(f"port-forward failed: {proc.stderr.read().decode()[:300]}", returncode=2)
            time.sleep(0.5)
    yield url
    proc.terminate()


@pytest.fixture(scope="session")
def keycloak_password():
    hub = os.getenv("HUB_KUBECONFIG") or os.getenv("SPOKE_KUBECONFIG") or os.getenv("KUBECONFIG")
    api = k8s.CoreV1Api(k8s_config.new_client_from_config(config_file=hub))
    data = api.read_namespaced_secret("keycloak-config", "keycloak").data
    return base64.b64decode(data["USER_PASSWORD"]).decode()


def subject(token: str) -> str:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))["sub"]


def claims(token: str) -> dict:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


@pytest.fixture(scope="session")
def user_token(keycloak_password):
    host = _env("KEYCLOAK_HOST")
    cache = {}

    def get(username):
        if username not in cache:
            r = httpx.post(
                f"https://{host}/keycloak/realms/platform/protocol/openid-connect/token",
                data={"client_id": "mcp-client", "username": username, "password": keycloak_password,
                      "grant_type": "password"},
                timeout=30,
            )
            r.raise_for_status()
            cache[username] = r.json()["access_token"]
        return cache[username]

    return get


@pytest.fixture(scope="session")
def sa_token(core):
    """A ServiceAccount token from the TokenRequest API, as the projected token would be."""

    def get(namespace, name, audience=AUDIENCE):
        req = k8s.AuthenticationV1TokenRequest(
            spec=k8s.V1TokenRequestSpec(audiences=[audience], expiration_seconds=1800)
        )
        return core.create_namespaced_service_account_token(name, namespace, req).status.token

    return get


@dataclass
class Gw:
    """Calls through the gateway with a given bearer token."""

    url: str

    def request(self, token, method, path, body=None, headers=None, timeout=60):
        h = dict(headers or {})
        if token:
            h["Authorization"] = f"Bearer {token}"
        if body is not None:
            h.setdefault("Content-Type", "application/json")
            h.setdefault("Accept", "application/json, text/event-stream")
        return httpx.request(method, self.url + path, headers=h, json=body, timeout=timeout)

    def chat(self, token, agent, context_id, message, headers=None, timeout=280):
        r = self.request(token, "POST", f"/{agent}/chat", {"message": message, "contextId": context_id},
                         headers=headers, timeout=timeout)
        try:
            d = r.json()
        except ValueError:
            return r.status_code, "<non-json>"
        return r.status_code, d.get("response") or f"ERROR:{d.get('error')}"


@pytest.fixture(scope="session")
def gw(gateway):
    return Gw(gateway)


@pytest.fixture(scope="session")
def logs(core, started):
    """Log text written since the run started, for all pods matching a label."""

    def get(namespace, name=None, selector=None, container=None):
        selector = selector or f"app.kubernetes.io/name={name}"
        since = max(1, int((datetime.now(timezone.utc) - started).total_seconds()) + 5)
        out = []
        for pod in core.list_namespaced_pod(namespace, label_selector=selector).items:
            try:
                out.append(core.read_namespaced_pod_log(
                    pod.metadata.name, namespace, since_seconds=since,
                    container=container or (name if name and any(c.name == name for c in pod.spec.containers) else None),
                ))
            except k8s.ApiException:
                pass
        return "\n".join(out)

    return get


@pytest.fixture(scope="session")
def gateway_logs(logs):
    return lambda: logs(GATEWAY_NS, selector=f"app.kubernetes.io/name={GATEWAY_SVC}")


def restarts(core, namespace, name):
    pods = core.list_namespaced_pod(namespace, label_selector=f"app.kubernetes.io/name={name}").items
    return sum((p.status.container_statuses or [k8s.V1ContainerStatus(name="", image="", image_id="", ready=False, restart_count=0)])[0].restart_count for p in pods)
