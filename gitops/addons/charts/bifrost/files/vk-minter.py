"""Platform minter for the harness judge virtual key.

Runs in namespace `bifrost` (PostSync hook and a low-frequency CronJob). It
signs in to Bifrost's management API with the admin credential, which is
mounted only in this namespace, and writes the resulting virtual key into a
Secret in the evaluation namespace. The Secret holds the key value only; the
admin credential never leaves namespace `bifrost`.

Rules:
- One key per configuration. The key name carries a hash of the models,
  budget and rate limit, so a changed value creates a new key.
- The Secret is written only when it changes. Any Bifrost or Kubernetes
  failure leaves it untouched and exits non-zero.
- Older keys with the prefix are deleted only when no harness Job is active.
  A running Job keeps the value it mounted, so deleting its key would fail
  its judge calls.
- Nothing printed contains a key value or the admin password.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

PREFIX = "oap-harness-judge-"
VALUE_PREFIX = "sk-bf-"
VK_PATH = "/api/governance/virtual-keys"
SECRET_KEY = "BIFROST_VIRTUAL_KEY"
ANN_KEY_NAME = "harness.oap.io/judge-key-name"
LABEL_RUN = "harness.oap.io/run=true"


class MinterError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def log(message: str) -> None:
    print(message, flush=True)


def vk_name(models, budget_usd, budget_reset, rl_requests, rl_reset) -> str:
    payload = json.dumps(
        {
            "models": sorted(models),
            "provider_key_ids": ["*"],
            "budget_usd": float(budget_usd),
            "budget_reset": budget_reset,
            "rate_limit_requests": int(rl_requests),
            "rate_limit_reset": rl_reset,
        },
        sort_keys=True,
    )
    return PREFIX + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]


class Http:
    """Minimal JSON client. Errors name the method, path and status only."""

    def __init__(self, base: str, headers: dict, context=None):
        self._base = base.rstrip("/")
        self._headers = headers
        self._context = context

    def request(self, method: str, path: str, body=None, params=None):
        url = self._base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        for key, value in self._headers.items():
            req.add_header(key, value)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=20, context=self._context) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise MinterError(f"{method} {path} returned HTTP {exc.code}", exc.code) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise MinterError(f"{method} {path} failed: {type(exc).__name__}") from None
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            raise MinterError(f"{method} {path}: reply is not JSON") from None


class Bifrost:
    def __init__(self, http):
        self._http = http

    def list_keys(self) -> list:
        reply = self._http.request("GET", VK_PATH, params={"search": PREFIX})
        if not isinstance(reply, dict) or "virtual_keys" not in reply:
            raise MinterError("list virtual keys: reply has no 'virtual_keys'")
        keys = reply["virtual_keys"] or []
        if not isinstance(keys, list):
            raise MinterError("list virtual keys: 'virtual_keys' is not a list")
        return [k for k in keys if isinstance(k, dict) and str(k.get("name", "")).startswith(PREFIX)]

    def create_key(self, name, models, budget_usd, budget_reset, rl_requests, rl_reset) -> dict:
        body = {
            "name": name,
            "description": "Agent harness judge key, owned by the bifrost chart minter. Do not edit by hand.",
            "provider_configs": [
                {"provider": "bedrock", "weight": 1, "allowed_models": list(models), "key_ids": ["*"]}
            ],
            "budgets": [{"max_limit": float(budget_usd), "reset_duration": budget_reset}],
            "rate_limit": {
                "request_max_limit": int(rl_requests),
                "request_reset_duration": rl_reset,
            },
            "is_active": True,
        }
        reply = self._http.request("POST", VK_PATH, body=body)
        key = reply.get("virtual_key") if isinstance(reply, dict) else None
        if not isinstance(key, dict):
            raise MinterError("create virtual key: reply has no 'virtual_key'")
        return key

    def delete_key(self, key_id) -> None:
        try:
            self._http.request("DELETE", f"{VK_PATH}/{key_id}")
        except MinterError as exc:
            if exc.status != 404:
                raise


class Kube:
    def __init__(self, http, namespace: str, secret: str):
        self._http = http
        self._ns = namespace
        self._secret = secret

    def _secret_path(self, name: str = "") -> str:
        base = f"/api/v1/namespaces/{self._ns}/secrets"
        return f"{base}/{name}" if name else base

    def read_secret(self):
        """(value, key name) of the stored key, or None when the Secret is absent."""
        try:
            sec = self._http.request("GET", self._secret_path(self._secret))
        except MinterError as exc:
            if exc.status == 404:
                return None
            raise
        ann = (sec.get("metadata") or {}).get("annotations") or {}
        raw = (sec.get("data") or {}).get(SECRET_KEY, "")
        try:
            value = base64.b64decode(raw).decode() if raw else ""
        except (ValueError, UnicodeDecodeError):
            value = ""
        return value, str(ann.get(ANN_KEY_NAME, ""))

    def write_secret(self, value: str, key_name: str, exists: bool) -> None:
        body = {
            "apiVersion": "v1",
            "kind": "Secret",
            "type": "Opaque",
            "metadata": {
                "name": self._secret,
                "namespace": self._ns,
                "labels": {"app.kubernetes.io/managed-by": "bifrost-vk-minter"},
                "annotations": {ANN_KEY_NAME: key_name},
            },
            "data": {SECRET_KEY: base64.b64encode(value.encode()).decode()},
        }
        if exists:
            self._http.request("PUT", self._secret_path(self._secret), body=body)
        else:
            self._http.request("POST", self._secret_path(), body=body)

    def harness_job_active(self) -> bool:
        reply = self._http.request(
            "GET", f"/apis/batch/v1/namespaces/{self._ns}/jobs", params={"labelSelector": LABEL_RUN}
        )
        for job in reply.get("items") or []:
            if (job.get("status") or {}).get("active"):
                return True
        return False


def run(bifrost, kube, config: dict) -> int:
    name = vk_name(
        config["models"], config["budget_usd"], config["budget_reset"],
        config["rl_requests"], config["rl_reset"],
    )
    try:
        stored = kube.read_secret()
        job_active = kube.harness_job_active()
        keys = bifrost.list_keys()
        known = stored if stored and stored[1] == name and stored[0].startswith(VALUE_PREFIX) else None
        value = ""
        for key in keys:
            if key.get("name") != name:
                continue
            candidate = str(key.get("value") or "")
            if candidate.startswith(VALUE_PREFIX):
                value = candidate
            elif known:
                value = known[0]
            else:
                # Ours by name, value unreadable here and in the Secret.
                # Replace it, but not while a Job holds the old value.
                if job_active:
                    raise MinterError(
                        f"key {name} has no readable value and a harness Job is active; "
                        "replaced when none is active"
                    )
                log(f"key {name} has no readable value; replacing it")
                bifrost.delete_key(key.get("id"))
                continue
            break
        if not value:
            created = bifrost.create_key(
                name, config["models"], config["budget_usd"], config["budget_reset"],
                config["rl_requests"], config["rl_reset"],
            )
            value = str(created.get("value") or "")
            if not value.startswith(VALUE_PREFIX):
                raise MinterError("create virtual key: the new key has no readable value")
            log(f"created key {name}")
        if not stored or stored[0] != value or stored[1] != name:
            kube.write_secret(value, name, exists=stored is not None)
            log("judge key Secret written")
        else:
            log("judge key Secret is current")
        if not job_active:
            for key in keys:
                if key.get("name") != name:
                    log("deleting a stale judge key")
                    bifrost.delete_key(key.get("id"))
        return 0
    except MinterError as exc:
        # Method, path and status only; never a body, key value or password.
        log(f"ERROR: {exc}")
        return 1


def _env_config() -> dict:
    return {
        "models": json.loads(os.environ["JUDGE_MODELS"]),
        "budget_usd": os.environ["JUDGE_BUDGET_USD"],
        "budget_reset": os.environ["JUDGE_BUDGET_RESET"],
        "rl_requests": os.environ["JUDGE_RATE_LIMIT_REQUESTS"],
        "rl_reset": os.environ["JUDGE_RATE_LIMIT_RESET"],
    }


def main() -> int:
    sa = "/var/run/secrets/kubernetes.io/serviceaccount"
    with open(f"{sa}/token") as fh:
        token = fh.read().strip()
    ctx = ssl.create_default_context(cafile=f"{sa}/ca.crt")
    kube_host = os.environ["KUBERNETES_SERVICE_HOST"]
    kube_port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    if ":" in kube_host:
        kube_host = f"[{kube_host}]"
    kube_http = Http(f"https://{kube_host}:{kube_port}", {"Authorization": f"Bearer {token}"}, ctx)
    basic = base64.b64encode(
        f"{os.environ['BIFROST_ADMIN_USERNAME']}:{os.environ['BIFROST_ADMIN_PASSWORD']}".encode()
    ).decode()
    bf_http = Http(os.environ["BIFROST_URL"], {"Authorization": f"Basic {basic}"})
    return run(
        Bifrost(bf_http),
        Kube(kube_http, os.environ["TARGET_NAMESPACE"], os.environ["SECRET_NAME"]),
        _env_config(),
    )


if __name__ == "__main__":
    sys.exit(main())
