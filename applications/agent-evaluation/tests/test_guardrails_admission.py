"""The agent component's guardrails block: admission and the model URL.
Rendered through the real definitions (helm template of the oam-agent-components
chart, then `vela dry-run --offline`). The platform policy is set with helm
values, the way the registry sets it on a cluster.
"""
from __future__ import annotations
import copy
import shutil
from pathlib import Path
import pytest
import yaml
from tests.test_agent_harness_rollout import _renderer

ROOT = Path(__file__).resolve().parents[3]
EXAMPLE = ROOT / "platform/oam/examples/example-agent-guardrails.yaml"
ROUTE = "http://agentgateway-proxy.agentgateway-system.svc.cluster.local:8080/llm/{}/v1"
BIFROST = "http://bifrost.bifrost.svc.cluster.local:8080/v1"
ON = ("--set-string", "global.guardrails.enabled=true")

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None or shutil.which("vela") is None,
    reason="helm and vela are needed to render the agent component",
)


def _app(guardrails=None, namespace="default", **props) -> dict:
    app = copy.deepcopy(yaml.safe_load(EXAMPLE.read_text(encoding="utf-8")))
    app["metadata"]["namespace"] = namespace
    p = app["spec"]["components"][0]["properties"]
    p.pop("guardrails", None)
    if guardrails is not None:
        p["guardrails"] = guardrails
    for k, v in props.items():
        p[k] = v
    return app


def _env(docs) -> dict:
    c = docs["Rollout"]["spec"]["template"]["spec"]["containers"][0]
    return {e["name"]: e.get("value") for e in c["env"]}


@pytest.fixture(scope="module")
def on(tmp_path_factory):
    return _renderer(tmp_path_factory.mktemp("gr-on"), *ON)


@pytest.fixture(scope="module")
def off(tmp_path_factory):
    return _renderer(tmp_path_factory.mktemp("gr-off"))


@pytest.fixture(scope="module")
def required(tmp_path_factory):
    return _renderer(
        tmp_path_factory.mktemp("gr-req"), *ON,
        "--set-string", "global.guardrails.requiredNamespaces=team-payments=strict,team-b=standard",
    )


def _rejected(out, *words):
    assert out.returncode != 0, out.stdout
    for w in words:
        assert w in out.stderr + out.stdout, (w, out.stderr)


def test_an_absent_block_and_enabled_false_keep_the_developer_url(on):
    for gr in (None, {"enabled": False}):
        docs = on(_app(gr))
        assert _env(docs)["LLM_GATEWAY_URL"] == BIFROST
        assert "agentgateway-token" not in str(docs["Rollout"])


def test_enabled_points_the_agent_at_the_profile_route(on):
    for profile in ("standard", "strict"):
        docs = on(_app({"enabled": True, "profile": profile}))
        assert _env(docs)["LLM_GATEWAY_URL"] == ROUTE.format(profile)


def test_the_profile_defaults_to_standard(on):
    assert _env(on(_app({"enabled": True})))["LLM_GATEWAY_URL"] == ROUTE.format("standard")


def test_the_route_url_ignores_a_developer_llm_gateway_url(on):
    app = _app({"enabled": True, "profile": "strict"},
               modelConfig={"modelId": "claude-sonnet", "llmGatewayUrl": "http://evil.example/v1"})
    assert _env(on(app))["LLM_GATEWAY_URL"] == ROUTE.format("strict")


def test_off_keeps_a_custom_llm_gateway_url(off):
    app = _app(None, modelConfig={"modelId": "claude-sonnet", "llmGatewayUrl": "http://x.example/v1"})
    assert _env(off(app))["LLM_GATEWAY_URL"] == "http://x.example/v1"


def test_a_cluster_without_guardrails_rejects_enabled(off):
    _rejected(off.fails(_app({"enabled": True})), "agent_guardrails")


def test_an_unknown_profile_is_rejected(on):
    _rejected(on.fails(_app({"enabled": True, "profile": "paranoid"})), "paranoid", "observe, standard, strict")


def test_a_profile_below_the_default_minimum_is_rejected_naming_it(on):
    _rejected(on.fails(_app({"enabled": True, "profile": "observe"})), "below the minimum standard")


def test_a_profile_below_a_namespace_minimum_is_rejected(required):
    _rejected(required.fails(_app({"enabled": True, "profile": "standard"}, namespace="team-payments")),
              "below the minimum strict", "team-payments")


def test_a_namespace_minimum_is_met_by_that_profile_or_stronger(required):
    assert _env(required(_app({"enabled": True, "profile": "strict"}, namespace="team-payments")))[
        "LLM_GATEWAY_URL"] == ROUTE.format("strict")
    assert _env(required(_app({"enabled": True, "profile": "standard"}, namespace="team-b")))[
        "LLM_GATEWAY_URL"] == ROUTE.format("standard")


def test_a_namespace_not_listed_uses_the_default_minimum(required):
    assert _env(required(_app({"enabled": True, "profile": "standard"}, namespace="elsewhere")))[
        "LLM_GATEWAY_URL"] == ROUTE.format("standard")


@pytest.mark.parametrize("gr", [{"enabled": False}, None])
def test_a_required_namespace_rejects_off_and_an_absent_block(required, gr):
    _rejected(required.fails(_app(gr, namespace="team-payments")), "requires guardrails", "strict")


def test_a_namespace_that_is_not_required_may_leave_guardrails_off(required):
    assert _env(required(_app({"enabled": False}, namespace="elsewhere")))["LLM_GATEWAY_URL"] == BIFROST


# The agent sends its projected agentgateway token to the guarded route. The same names,
# path and value as the gateway-identity trait, so that trait and this block merge.
TOKEN_ENV = {"WORKLOAD_TOKEN_PATH": "/var/run/secrets/agentgateway/token",
             "LLM_GATEWAY_AUTH_MODE": "workload-token"}


def _pod(docs) -> dict:
    return docs["Rollout"]["spec"]["template"]["spec"]


def test_guardrails_on_mounts_the_token_and_sets_both_env_vars(on):
    docs = on(_app({"enabled": True}))
    pod = _pod(docs)
    assert pod["volumes"] == [{"name": "agentgateway-token", "projected": {"sources": [
        {"serviceAccountToken": {"audience": "agentgateway", "expirationSeconds": 3600, "path": "token"}}]}}]
    assert pod["containers"][0]["volumeMounts"] == [
        {"name": "agentgateway-token", "mountPath": "/var/run/secrets/agentgateway", "readOnly": True}]
    env = _env(docs)
    assert {k: env.get(k) for k in TOKEN_ENV} == TOKEN_ENV


@pytest.mark.parametrize("gr", [None, {"enabled": False}])
def test_guardrails_off_renders_no_token_volume_mount_or_env(on, gr):
    docs = on(_app(gr))
    pod = _pod(docs)
    assert "volumes" not in pod and "volumeMounts" not in pod["containers"][0]
    assert not set(TOKEN_ENV) & set(_env(docs))


def test_guardrails_plus_the_gateway_identity_trait_merge_to_one_volume_and_env(on):
    app = _app({"enabled": True})
    app["spec"]["components"][0]["traits"] = [{"type": "gateway-identity", "properties": {}}]
    docs = on(app)
    pod = _pod(docs)
    assert [v["name"] for v in pod["volumes"]] == ["agentgateway-token"]
    assert [m["name"] for m in pod["containers"][0]["volumeMounts"]] == ["agentgateway-token"]
    names = [e["name"] for e in pod["containers"][0]["env"]]
    assert names.count("WORKLOAD_TOKEN_PATH") == 1 and names.count("LLM_GATEWAY_AUTH_MODE") == 1


def test_a_confidential_agent_still_renders_with_static_token_env(on):
    docs = on(_app({"enabled": True}, sandbox=True, sandboxMode="confidential",
                         cedarPolicy="A" * 64))
    env = {e["name"]: e for e in _pod(docs)["containers"][0]["env"]}
    assert env["LLM_GATEWAY_URL"]["value"] == ROUTE.format("standard")
    assert env["WORKLOAD_TOKEN_PATH"]["value"] == TOKEN_ENV["WORKLOAD_TOKEN_PATH"]
    assert "valueFrom" not in env["WORKLOAD_TOKEN_PATH"] and "valueFrom" not in env["LLM_GATEWAY_AUTH_MODE"]
