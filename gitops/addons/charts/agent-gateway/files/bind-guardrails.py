"""Binds the Bedrock guardrail ID and version into the gateway's prompt guard policies.

Runs as a PostSync hook Job and as a CronJob in the agent-gateway release namespace. For
each profile it waits until the Crossplane Guardrail and GuardrailVersion are Ready and in
sync, reads the guardrail ID (status.atProvider.guardrailId) and the version number
(status.atProvider.version), and writes them into that profile's AgentgatewayPolicy with a
JSON patch that tests the old values first, so a concurrent change fails the patch instead
of being overwritten.

Rules:
- Nothing is written until both objects are Ready and Synced and the version was made from
  the current spec (its observed description equals the desired one). A version left over
  from an earlier catalog revision is not bound.
- A policy already holding the right values is left alone.
- Any failure leaves the policies as they are and, after the deadline, exits non-zero.
- Log lines carry profile names and version numbers only. The Job handles no request data.
"""
from __future__ import annotations
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
GUARDRAIL_API = "/apis/bedrock.aws.upbound.io/v1beta1"
POLICY_API = "/apis/agentgateway.dev/v1alpha1"
PLACEHOLDER = "pending"
class BindError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status
def log(message: str) -> None:
    print(message, flush=True)
class Http:
    """Minimal JSON client. Errors name the method, path and status only."""
    def __init__(self, base: str, headers: dict, context=None):
        self._base = base.rstrip("/")
        self._headers = headers
        self._context = context
    def request(self, method: str, path: str, body=None, content_type="application/json"):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self._base + path, data=data, method=method)
        for key, value in self._headers.items():
            req.add_header(key, value)
        if data is not None:
            req.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(req, timeout=20, context=self._context) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise BindError(f"{method} {path} returned HTTP {exc.code}", exc.code) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise BindError(f"{method} {path} failed: {type(exc).__name__}") from None
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            raise BindError(f"{method} {path}: reply is not JSON") from None
def _condition(obj: dict, kind: str) -> str:
    for cond in (obj.get("status") or {}).get("conditions") or []:
        if cond.get("type") == kind:
            return str(cond.get("status", ""))
    return ""
def _ready(obj: dict) -> bool:
    return _condition(obj, "Ready") == "True" and _condition(obj, "Synced") == "True"
class Kube:
    def __init__(self, http, policy_namespace: str):
        self._http = http
        self._ns = policy_namespace
    def _get(self, path: str):
        """The object, or None when it does not exist yet."""
        try:
            return self._http.request("GET", path)
        except BindError as exc:
            if exc.status == 404:
                return None
            raise
    def guardrail(self, name: str):
        return self._get(f"{GUARDRAIL_API}/guardrails/{name}")
    def guardrail_version(self, name: str):
        return self._get(f"{GUARDRAIL_API}/guardrailversions/{name}")
    def _policy_path(self, name: str) -> str:
        return f"{POLICY_API}/namespaces/{self._ns}/agentgatewaypolicies/{name}"
    def policy(self, name: str):
        return self._get(self._policy_path(name))
    def patch_policy(self, name: str, ops: list) -> None:
        self._http.request("PATCH", self._policy_path(name), body=ops,
                           content_type="application/json-patch+json")
def resolve(kube, guardrail_name: str):
    """(guardrail ID, version) when both objects are ready, else a reason string."""
    guardrail = kube.guardrail(guardrail_name)
    if guardrail is None:
        return None, f"Guardrail {guardrail_name} not found"
    if not _ready(guardrail):
        return None, f"Guardrail {guardrail_name} not Ready and Synced"
    at = (guardrail.get("status") or {}).get("atProvider") or {}
    guardrail_id = at.get("guardrailId") or at.get("id") or ""
    if not guardrail_id:
        return None, f"Guardrail {guardrail_name} has no ID yet"
    version = kube.guardrail_version(guardrail_name)
    if version is None:
        return None, f"GuardrailVersion {guardrail_name} not found"
    if not _ready(version):
        return None, f"GuardrailVersion {guardrail_name} not Ready and Synced"
    observed = (version.get("status") or {}).get("atProvider") or {}
    wanted = ((version.get("spec") or {}).get("forProvider") or {}).get("description")
    if wanted is not None and observed.get("description") != wanted:
        return None, f"GuardrailVersion {guardrail_name} is not yet made from the current catalog revision"
    number = str(observed.get("version") or "")
    if not number.isdigit():
        return None, f"GuardrailVersion {guardrail_name} has no version number yet"
    return (guardrail_id, number), ""
def _guard_index(policy: dict) -> int:
    entries = (((policy.get("spec") or {}).get("backend") or {}).get("ai") or {}).get(
        "promptGuard", {}).get("request") or []
    for i, entry in enumerate(entries):
        if isinstance(entry, dict) and "bedrockGuardrails" in entry:
            return i
    raise BindError("policy has no bedrockGuardrails request guard")
def bind(kube, profile: dict):
    """Write the ID and version into the profile's policy. True when bound."""
    values, reason = resolve(kube, profile["guardrail"])
    if values is None:
        return False, reason
    policy = kube.policy(profile["policy"])
    if policy is None:
        return False, f"policy {profile['policy']} not found"
    index = _guard_index(policy)
    current = policy["spec"]["backend"]["ai"]["promptGuard"]["request"][index]["bedrockGuardrails"]
    guardrail_id, number = values
    if current.get("identifier") == guardrail_id and current.get("version") == number:
        return True, ""
    base = f"/spec/backend/ai/promptGuard/request/{index}/bedrockGuardrails"
    ops = [
        {"op": "test", "path": f"{base}/identifier", "value": current.get("identifier")},
        {"op": "test", "path": f"{base}/version", "value": current.get("version")},
        {"op": "replace", "path": f"{base}/identifier", "value": guardrail_id},
        {"op": "replace", "path": f"{base}/version", "value": number},
    ]
    kube.patch_policy(profile["policy"], ops)
    log(f"bound profile {profile['profile']} to guardrail version {number}")
    return True, ""
def run(kube, profiles: list, deadline_s: float, poll_s: float, sleep=time.sleep, clock=time.monotonic) -> int:
    pending = {p["profile"]: p for p in profiles}
    reasons: dict = {}
    end = clock() + deadline_s
    while pending:
        for name, profile in list(pending.items()):
            try:
                done, reason = bind(kube, profile)
            except BindError as exc:
                done, reason = False, str(exc)
            if done:
                del pending[name]
                reasons.pop(name, None)
            else:
                reasons[name] = reason
        if not pending:
            break
        if clock() >= end:
            for name in sorted(pending):
                log(f"profile {name} not bound: {reasons.get(name, 'unknown')}")
            return 1
        sleep(poll_s)
    log("all guardrail profiles bound")
    return 0
def main() -> int:
    sa = "/var/run/secrets/kubernetes.io/serviceaccount"
    with open(f"{sa}/token") as fh:
        token = fh.read().strip()
    ctx = ssl.create_default_context(cafile=f"{sa}/ca.crt")
    kube_host = os.environ["KUBERNETES_SERVICE_HOST"]
    kube_port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    if ":" in kube_host:
        kube_host = f"[{kube_host}]"
    http = Http(f"https://{kube_host}:{kube_port}", {"Authorization": f"Bearer {token}"}, ctx)
    profiles = json.loads(os.environ["GUARDRAIL_PROFILES"])
    return run(
        Kube(http, os.environ["POLICY_NAMESPACE"]),
        profiles,
        float(os.environ.get("DEADLINE_SECONDS", "600")),
        float(os.environ.get("POLL_SECONDS", "10")),
    )
if __name__ == "__main__":
    sys.exit(main())
