"""agent-gateway guardrail objects: render only with the flag on, scoped and namespaced."""
from __future__ import annotations
import json
import re
from tests._guardrails_helm import CHARTS, needs_helm, of_kind, render
pytestmark = needs_helm
CHART = "agent-gateway"
OFF = ["--set", "global.clusterName=c1", "--set", "global.awsRegion=us-west-2",
       "--set-string", "global.awsAccountId=111122223333"]
ON = OFF + ["--set-string", "guardrails.enabled=true"]
def test_flag_off_renders_no_guardrail_role():
    docs = render(CHART, *OFF)
    assert of_kind(docs, "Role") == [] and of_kind(docs, "PodIdentityAssociation") == []
def _policy(docs):
    role = of_kind(docs, "Role")[0]
    return json.loads(role["spec"]["forProvider"]["inlinePolicy"][0]["policy"])
def test_flag_on_renders_role_and_association_for_the_proxy():
    docs = render(CHART, *ON)
    assert len(of_kind(docs, "Role")) == 1
    assoc = of_kind(docs, "PodIdentityAssociation")[0]["spec"]["forProvider"]
    assert assoc["namespace"] == "agentgateway-system"
    assert assoc["serviceAccount"] == "agentgateway-proxy"
    assert assoc["clusterName"] == "c1" and assoc["region"] == "us-west-2"
def test_policy_allows_only_apply_guardrail():
    policy = _policy(render(CHART, *ON))
    actions = [a for st in policy["Statement"] for a in st["Action"]]
    assert set(actions) == {"bedrock:ApplyGuardrail"}
    first = policy["Statement"][0]
    assert first["Resource"] == ["arn:aws:bedrock:us-west-2:111122223333:guardrail/*"]
    assert first["Condition"] == {"StringEquals": {"aws:ResourceTag/platform.gitops.io/cluster": "c1"}}
    assert "*" not in [r for st in policy["Statement"] for r in st["Resource"]]
def test_policy_allows_the_cross_region_profile_and_drops_it_for_classic():
    policy = _policy(render(CHART, *ON))
    profile = policy["Statement"][1]["Resource"]
    assert profile == ["arn:aws:bedrock:*:111122223333:guardrail-profile/us.guardrail.v1:0"]
    classic = _policy(render(CHART, *ON, "--set", "guardrails.crossRegion.profileId="))
    assert len(classic["Statement"]) == 1
def test_missing_account_fails_when_enabled():
    from tests._guardrails_helm import run
    out = run(CHART, "--set", "global.clusterName=c1", "--set", "global.awsRegion=us-west-2",
              "--set-string", "guardrails.enabled=true")
    assert out.returncode != 0 and "awsAccountId" in out.stderr
def test_template_has_no_literal_account_or_region():
    text = (CHARTS / "agent-gateway/templates/guardrail-iam.yaml").read_text(encoding="utf-8")
    assert not re.search(r"\b\d{12}\b", text)
    assert not re.search(r"\b(us|eu|ap|sa|ca|me|af)-[a-z]+-\d\b", text)
def test_both_charts_name_the_same_cross_region_profile():
    import yaml
    gw = yaml.safe_load((CHARTS / "agent-gateway/values.yaml").read_text())
    xp = yaml.safe_load((CHARTS / "crossplane-agentcore/values.yaml").read_text())
    assert gw["guardrails"]["crossRegion"]["profileId"] == xp["guardrails"]["crossRegion"]["profileId"]
