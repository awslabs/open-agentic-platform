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
# Guardrail and GuardrailVersion objects (05-guardrails.yaml).
def _guardrails(*extra):
    docs = render("crossplane-agentcore", *ON, *extra)
    return (
        {d["metadata"]["name"]: d for d in of_kind(docs, "Guardrail")},
        {d["metadata"]["name"]: d for d in of_kind(docs, "GuardrailVersion")},
    )
def test_flag_off_renders_no_guardrails():
    docs = render("crossplane-agentcore", *BASE)
    assert of_kind(docs, "Guardrail") == [] and of_kind(docs, "GuardrailVersion") == []
def test_flag_on_renders_one_guardrail_and_version_per_profile():
    guardrails, versions = _guardrails()
    assert set(guardrails) == set(versions) == {"c1-observe", "c1-standard", "c1-strict"}
    for name, g in guardrails.items():
        fp = g["spec"]["forProvider"]
        assert fp["region"] == "us-west-2"
        assert fp["blockedInputMessaging"] and fp["blockedOutputsMessaging"]
        assert fp["tags"]["platform.gitops.io/cluster"] == "c1"
        assert fp["crossRegionConfig"]["guardrailProfileIdentifier"] == "us.guardrail.v1:0"
        assert fp["contentPolicyConfig"]["tierConfig"]["tierName"] == "STANDARD"
        v = versions[name]["spec"]["forProvider"]
        assert v["guardrailArnRef"]["name"] == name
        assert v["region"] == "us-west-2"
def _filters(g):
    return {f["type"]: f for f in g["spec"]["forProvider"]["contentPolicyConfig"]["filtersConfig"]}
def test_observe_has_only_low_strength_filters():
    g = _guardrails()[0]["c1-observe"]
    assert {f["inputStrength"] for f in _filters(g).values()} == {"LOW"}
    assert "PROMPT_ATTACK" not in _filters(g)
    assert "sensitiveInformationPolicyConfig" not in g["spec"]["forProvider"]
def test_standard_has_prompt_attack_and_anonymizes_pii():
    fp = _guardrails()[0]["c1-standard"]["spec"]["forProvider"]
    attack = _filters(_guardrails()[0]["c1-standard"])["PROMPT_ATTACK"]
    assert attack["inputStrength"] == "MEDIUM" and attack["outputStrength"] == "NONE"
    pii = fp["sensitiveInformationPolicyConfig"]["piiEntitiesConfig"]
    assert pii and {p["action"] for p in pii} == {"ANONYMIZE"}
    assert "topicPolicyConfig" not in fp
def test_strict_has_a_denied_topic_and_high_strength():
    g = _guardrails()[0]["c1-strict"]
    topics = g["spec"]["forProvider"]["topicPolicyConfig"]["topicsConfig"]
    assert topics and all(t["type"] == "DENY" for t in topics)
    assert g["spec"]["forProvider"]["topicPolicyConfig"]["tierConfig"]["tierName"] == "STANDARD"
    assert {f["inputStrength"] for f in _filters(g).values()} == {"HIGH"}
def test_catalog_revision_is_in_the_version_description():
    versions = _guardrails("--set-string", "guardrails.catalogRevision=7")[1]
    assert "revision 7" in versions["c1-standard"]["spec"]["forProvider"]["description"]
def test_provider_role_can_use_the_cross_region_profile():
    docs = render("crossplane-agentcore", *ON)
    role = next(d for d in of_kind(docs, "Role") if d["metadata"]["name"] == "c1-bedrock-provider-role")
    policy = json.loads(role["spec"]["forProvider"]["inlinePolicy"][0]["policy"])
    resources = policy["Statement"][0]["Resource"]
    assert "arn:aws:bedrock:us-west-2:111122223333:guardrail-profile/us.guardrail.v1:0" in resources
def test_classic_tier_omits_the_cross_region_config():
    g = _guardrails("--set", "guardrails.tier=CLASSIC", "--set", "guardrails.crossRegion.profileId=")[0]["c1-standard"]
    fp = g["spec"]["forProvider"]
    assert "crossRegionConfig" not in fp and "tierConfig" not in fp["contentPolicyConfig"]
def test_standard_tier_without_a_profile_fails():
    out = run("crossplane-agentcore", *ON, "--set", "guardrails.crossRegion.profileId=")
    assert out.returncode != 0 and "profileId" in out.stderr
def test_unknown_profile_name_fails():
    out = run("crossplane-agentcore", *ON, "--set", "guardrails.profiles.extra.contentFilters.HATE=LOW")
    assert out.returncode != 0 and "guardrails.profiles.extra is not listed in guardrails.order" in out.stderr
def test_order_entry_without_a_profile_fails():
    out = run("crossplane-agentcore", *ON, "--set", "guardrails.order={observe,standard,strict,paranoid}")
    assert out.returncode != 0 and "paranoid" in out.stderr
def test_unknown_detector_key_fails():
    out = run("crossplane-agentcore", *ON, "--set", "guardrails.profiles.observe.badKey=1")
    assert out.returncode != 0 and "unknown key" in out.stderr
def test_unknown_filter_type_and_strength_fail():
    out = run("crossplane-agentcore", *ON, "--set", "guardrails.profiles.observe.contentFilters.BORING=LOW")
    assert out.returncode != 0 and "BORING" in out.stderr
    out = run("crossplane-agentcore", *ON, "--set", "guardrails.profiles.observe.contentFilters.HATE=EXTREME")
    assert out.returncode != 0 and "EXTREME" in out.stderr
def test_rendered_output_parses_as_yaml():
    render("crossplane-agentcore", *ON)
