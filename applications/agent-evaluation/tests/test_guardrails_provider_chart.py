"""crossplane-agentcore renders provider-aws-bedrock only when guardrails are enabled."""
from __future__ import annotations
import json
from tests._guardrails_helm import needs_helm, of_kind, render, render_text, run
pytestmark = needs_helm
BASE = ["--set", "global.eksClusterName=c1"]
ON = BASE + ["--set-string", "guardrails.enabled=true", "--set-string", "guardrails.awsAccountId=111122223333"]
def _bedrock(docs):
    return [d for d in docs if "bedrock-provider" in d["metadata"]["name"] or d["metadata"]["name"] == "provider-aws-bedrock"]
def test_flag_off_renders_no_bedrock_provider():
    docs = render("crossplane-agentcore", *BASE)
    assert _bedrock(docs) == []
    assert "kind: GuardrailVersion" not in render_text("crossplane-agentcore", *BASE)
def test_flag_on_renders_provider_objects():
    docs = render("crossplane-agentcore", *ON)
    names = {(d["kind"], d["metadata"]["name"]) for d in _bedrock(docs)}
    assert names == {
        ("ServiceAccount", "provider-aws-bedrock"),
        ("DeploymentRuntimeConfig", "bedrock-provider-runtime"),
        ("Provider", "provider-aws-bedrock"),
        ("Role", "c1-bedrock-provider-role"),
        ("PodIdentityAssociation", "c1-bedrock-provider"),
    }
def test_runtime_config_and_association_name_the_same_service_account():
    docs = render("crossplane-agentcore", *ON)
    drc = next(d for d in of_kind(docs, "DeploymentRuntimeConfig") if d["metadata"]["name"] == "bedrock-provider-runtime")
    assoc = next(d for d in of_kind(docs, "PodIdentityAssociation") if d["metadata"]["name"] == "c1-bedrock-provider")
    sa = drc["spec"]["deploymentTemplate"]["spec"]["template"]["spec"]["serviceAccountName"]
    assert sa == assoc["spec"]["forProvider"]["serviceAccount"] == "provider-aws-bedrock"
    container = drc["spec"]["deploymentTemplate"]["spec"]["template"]["spec"]["containers"][0]
    assert container["name"] == "package-runtime"
    assert {e["name"] for e in container["env"]} == {"GOMEMLIMIT", "GOGC"}
def test_bedrock_role_is_separate_from_the_agentcore_role():
    docs = render("crossplane-agentcore", *ON)
    roles = {d["metadata"]["name"]: d for d in of_kind(docs, "Role")}
    assert "c1-agentcore-provider-role" in roles and "c1-bedrock-provider-role" in roles
    bedrock = roles["c1-bedrock-provider-role"]
    assert bedrock["metadata"]["labels"]["platform.gitops.io/role"] == "bedrock-provider"
    assoc = next(d for d in of_kind(docs, "PodIdentityAssociation") if d["metadata"]["name"] == "c1-bedrock-provider")
    selector = assoc["spec"]["forProvider"]["roleArnSelector"]["matchLabels"]
    assert selector["platform.gitops.io/role"] == "bedrock-provider"
def test_provider_policy_is_scoped():
    docs = render("crossplane-agentcore", *ON)
    role = next(d for d in of_kind(docs, "Role") if d["metadata"]["name"] == "c1-bedrock-provider-role")
    policy = json.loads(role["spec"]["forProvider"]["inlinePolicy"][0]["policy"])
    seen = set()
    for st in policy["Statement"]:
        for action in st["Action"]:
            assert action != "bedrock:*" and not action.endswith(":*")
            seen.add(action)
        if st["Action"] != ["bedrock:ListGuardrails"]:
            resources = st["Resource"] if isinstance(st["Resource"], list) else [st["Resource"]]
            assert "*" not in resources
            assert all(r.startswith("arn:aws:bedrock:us-west-2:111122223333:guardrail") for r in resources)
    assert seen == {
        "bedrock:CreateGuardrail", "bedrock:CreateGuardrailVersion", "bedrock:GetGuardrail",
        "bedrock:UpdateGuardrail", "bedrock:DeleteGuardrail", "bedrock:ListGuardrails",
        "bedrock:ListTagsForResource", "bedrock:TagResource", "bedrock:UntagResource",
    }
def test_provider_package_uses_the_values_version_and_skips_dependency_resolution():
    docs = render("crossplane-agentcore", *ON, "--set", "guardrails.provider.version=v2.9.0")
    provider = next(d for d in of_kind(docs, "Provider") if d["metadata"]["name"] == "provider-aws-bedrock")
    assert provider["spec"]["package"] == "xpkg.upbound.io/upbound/provider-aws-bedrock:v2.9.0"
    assert provider["spec"]["skipDependencyResolution"] is True
    assert provider["spec"]["runtimeConfigRef"]["name"] == "bedrock-provider-runtime"
def test_provider_below_v2_9_0_is_rejected():
    out = run("crossplane-agentcore", *ON, "--set", "guardrails.provider.version=v2.8.0")
    assert out.returncode != 0 and "v2.9.0" in out.stderr
def test_account_id_is_required_when_enabled():
    out = run("crossplane-agentcore", *BASE, "--set-string", "guardrails.enabled=true")
    assert out.returncode != 0 and "awsAccountId" in out.stderr
