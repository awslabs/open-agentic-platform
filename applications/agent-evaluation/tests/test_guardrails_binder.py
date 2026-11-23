"""Prompt guard policies and the Job that binds guardrail IDs into them."""
from __future__ import annotations
import importlib.util
import json
from pathlib import Path
import pytest
import yaml
from tests._guardrails_helm import CHARTS, ROOT, needs_helm, of_kind, render, render_text
SCRIPT = CHARTS / "agent-gateway/files/bind-guardrails.py"
spec = importlib.util.spec_from_file_location("bind_guardrails", SCRIPT)
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)
CHART = "agent-gateway"
OFF = ["--set", "global.clusterName=c1", "--set", "global.awsRegion=us-west-2",
       "--set-string", "global.awsAccountId=111122223333"]
ON = OFF + ["--set-string", "guardrails.enabled=true"]
def _guards(docs):
    return {d["metadata"]["name"]: d for d in of_kind(docs, "AgentgatewayPolicy")
            if d["metadata"]["name"].endswith("-guard")}
@needs_helm
def test_flag_off_renders_no_guard_or_binder():
    text = render_text(CHART, *OFF)
    assert "guardrail-binder" not in text and "promptGuard" not in text
@needs_helm
def test_every_guard_is_request_only_bedrock_fail_closed_with_explicit_scope():
    guards = _guards(render(CHART, *ON))
    assert set(guards) == {"llm-observe-guard", "llm-standard-guard", "llm-strict-guard"}
    for name, d in guards.items():
        pg = d["spec"]["backend"]["ai"]["promptGuard"]
        assert set(pg) == {"request"}  # no response side, no streaming switch
        assert len(pg["request"]) == 1
        entry = pg["request"][0]
        assert "webhook" not in entry
        assert set(entry) == {"bedrockGuardrails", "scope", "response"}
        bg = entry["bedrockGuardrails"]
        assert bg["failureMode"] == "FailClosed"
        assert entry["scope"] == ["Messages", "ToolOutput"]
        assert bg["region"] == "us-west-2"
        assert bg["policies"] == {"auth": {"aws": {}}}
        assert bg["identifier"] == "pending" and bg["version"] == "pending"
        assert d["metadata"]["namespace"] == "agentgateway-system"
        assert d["spec"]["targetRefs"][0]["name"] == name[: -len("-guard")]
@needs_helm
def test_only_observe_audits():
    guards = _guards(render(CHART, *ON))
    actions = {n: g["spec"]["backend"]["ai"]["promptGuard"]["request"][0]["bedrockGuardrails"]["action"]
               for n, g in guards.items()}
    assert actions == {"llm-observe-guard": "Audit", "llm-standard-guard": "Reject", "llm-strict-guard": "Reject"}
@needs_helm
def test_missing_explicit_fields_fail_the_render():
    from tests._guardrails_helm import run
    out = run(CHART, *ON, "--set", "guardrails.profiles.standard.failureMode=")
    assert out.returncode != 0 and "failureMode" in out.stderr
@needs_helm
def test_binder_renders_least_privilege_rbac_and_hook():
    docs = render(CHART, *ON)
    cr = of_kind(docs, "ClusterRole")[0]["rules"]
    assert cr == [{"apiGroups": ["bedrock.aws.upbound.io"], "resources": ["guardrails", "guardrailversions"],
                   "resourceNames": ["c1-observe", "c1-standard", "c1-strict"], "verbs": ["get"]}]
    role = of_kind(docs, "Role")[0]
    assert role["metadata"]["namespace"] == "agentgateway-system"
    assert role["rules"][0]["verbs"] == ["get", "patch"]
    assert role["rules"][0]["resourceNames"] == ["llm-observe-guard", "llm-standard-guard", "llm-strict-guard"]
    job = of_kind(docs, "Job")[0]
    assert job["metadata"]["annotations"]["argocd.argoproj.io/hook"] == "PostSync"
    assert len(of_kind(docs, "CronJob")) == 1
    cm = of_kind(docs, "ConfigMap")[0]
    assert cm["data"]["bind-guardrails.py"].startswith('"""Binds the Bedrock guardrail')
@needs_helm
def test_binder_names_match_the_guardrails_crossplane_creates():
    gw = render(CHART, *ON)
    profiles = json.loads(next(e["value"] for e in of_kind(gw, "Job")[0]["spec"]["template"]["spec"]["containers"][0]["env"]
                               if e["name"] == "GUARDRAIL_PROFILES"))
    xp = render("crossplane-agentcore", "--set", "global.eksClusterName=c1",
                "--set-string", "guardrails.enabled=true", "--set-string", "guardrails.awsAccountId=1")
    created = {d["metadata"]["name"] for d in of_kind(xp, "Guardrail")}
    assert {p["guardrail"] for p in profiles} == created
    policies = {d["metadata"]["name"] for d in _guards(gw).values()}
    assert {p["policy"] for p in profiles} == policies
def test_registry_ignores_the_bound_fields_and_keeps_the_default_sync_policy():
    entry = yaml.safe_load((ROOT / "gitops/addons/registry/gateway.yaml").read_text())["agent-gateway"]
    defaults = yaml.safe_load((ROOT / "gitops/addons/registry/_defaults.yaml").read_text())["syncPolicy"]
    ignored = [r for r in entry["ignoreDifferences"] if r["kind"] == "AgentgatewayPolicy"]
    assert ignored[0]["jsonPointers"] == [
        "/spec/backend/ai/promptGuard/request/0/bedrockGuardrails/identifier",
        "/spec/backend/ai/promptGuard/request/0/bedrockGuardrails/version",
    ]
    sync = entry["syncPolicy"]
    assert "RespectIgnoreDifferences=true" in sync["syncOptions"]
    stripped = dict(sync, syncOptions=[o for o in sync["syncOptions"] if o != "RespectIgnoreDifferences=true"])
    assert stripped == defaults
# The binder script, with a fake Kubernetes API.
PROFILE = {"profile": "standard", "guardrail": "c1-standard", "policy": "llm-standard-guard"}
def _obj(ready=True, synced=True, **at):
    conds = [{"type": "Ready", "status": "True" if ready else "False"},
             {"type": "Synced", "status": "True" if synced else "False"}]
    return {"status": {"conditions": conds, "atProvider": at}}
def _policy(identifier="pending", version="pending"):
    return {"spec": {"backend": {"ai": {"promptGuard": {"request": [
        {"bedrockGuardrails": {"identifier": identifier, "version": version, "failureMode": "FailClosed"}}]}}}}}
class FakeKube:
    def __init__(self, guardrail=None, version=None, policy=None):
        self.g, self.v, self.p = guardrail, version, policy
        self.patches = []
    def guardrail(self, name):
        return self.g
    def guardrail_version(self, name):
        return self.v
    def policy(self, name):
        return self.p
    def patch_policy(self, name, ops):
        self.patches.append((name, ops))
def _ready_kube(policy=None):
    version = _obj(version="3", description="standard catalog revision 1")
    version["spec"] = {"forProvider": {"description": "standard catalog revision 1"}}
    return FakeKube(_obj(guardrailId="abc123"), version, policy or _policy())
def test_bind_patches_the_right_policy_with_the_id_and_version():
    kube = _ready_kube()
    ok, _ = b.bind(kube, PROFILE)
    assert ok
    name, ops = kube.patches[0]
    assert name == "llm-standard-guard"
    base = "/spec/backend/ai/promptGuard/request/0/bedrockGuardrails"
    assert ops == [
        {"op": "test", "path": f"{base}/identifier", "value": "pending"},
        {"op": "test", "path": f"{base}/version", "value": "pending"},
        {"op": "replace", "path": f"{base}/identifier", "value": "abc123"},
        {"op": "replace", "path": f"{base}/version", "value": "3"},
    ]
def test_bind_leaves_an_up_to_date_policy_alone():
    kube = _ready_kube(_policy("abc123", "3"))
    assert b.bind(kube, PROFILE) == (True, "")
    assert kube.patches == []
@pytest.mark.parametrize("make", [
    lambda: FakeKube(None, None, _policy()),
    lambda: FakeKube(_obj(ready=False, guardrailId="x"), None, _policy()),
    lambda: FakeKube(_obj(guardrailId=""), None, _policy()),
    lambda: FakeKube(_obj(guardrailId="x"), None, _policy()),
    lambda: FakeKube(_obj(guardrailId="x"), _obj(synced=False, version="1"), _policy()),
    lambda: FakeKube(_obj(guardrailId="x"), _obj(version=""), _policy()),
])
def test_nothing_is_bound_until_both_objects_are_ready(make):
    kube = make()
    ok, reason = b.bind(kube, PROFILE)
    assert not ok and reason
    assert kube.patches == []
def test_a_version_from_an_older_revision_is_not_bound():
    kube = _ready_kube()
    kube.v["spec"] = {"forProvider": {"description": "standard catalog revision 2"}}
    ok, reason = b.bind(kube, PROFILE)
    assert not ok and "current catalog revision" in reason and kube.patches == []
def test_missing_policy_is_retried_not_created():
    kube = _ready_kube()
    kube.p = None
    ok, reason = b.bind(kube, PROFILE)
    assert not ok and "not found" in reason and kube.patches == []
class Clock:
    def __init__(self):
        self.now = 0.0
    def __call__(self):
        return self.now
    def sleep(self, seconds):
        self.now += seconds
def test_run_retries_while_not_ready_then_binds():
    kube = FakeKube(None, None, _policy())
    clock = Clock()
    ready = _ready_kube()
    def sleeping(seconds):
        clock.sleep(seconds)
        if clock.now >= 30:
            kube.g, kube.v = ready.g, ready.v
    assert b.run(kube, [PROFILE], 600, 10, sleep=sleeping, clock=clock) == 0
    assert len(kube.patches) == 1 and clock.now >= 30
def test_run_exits_non_zero_after_the_deadline(capsys):
    kube = FakeKube(None, None, _policy())
    clock = Clock()
    assert b.run(kube, [PROFILE], 60, 10, sleep=clock.sleep, clock=clock) == 1
    out = capsys.readouterr().out
    assert "profile standard not bound: Guardrail c1-standard not found" in out
    assert kube.patches == []
def test_run_treats_an_api_error_as_not_ready():
    class Broken(FakeKube):
        def guardrail(self, name):
            raise b.BindError("GET /x returned HTTP 500", 500)
    clock = Clock()
    assert b.run(Broken(None, None, _policy()), [PROFILE], 30, 10, sleep=clock.sleep, clock=clock) == 1
def test_log_lines_carry_no_guardrail_id(capsys):
    kube = _ready_kube()
    b.bind(kube, PROFILE)
    b.run(_ready_kube(), [PROFILE], 10, 1)
    out = capsys.readouterr().out
    assert "abc123" not in out and "bound profile standard to guardrail version 3" in out
