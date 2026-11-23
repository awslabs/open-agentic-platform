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
def _iam_roles(docs):
    return [d for d in of_kind(docs, "Role") if d["apiVersion"].startswith("iam.aws.upbound.io")]
def test_flag_off_renders_no_guardrail_role():
    docs = render(CHART, *OFF)
    assert _iam_roles(docs) == [] and of_kind(docs, "PodIdentityAssociation") == []
def _policy(docs):
    role = _iam_roles(docs)[0]
    return json.loads(role["spec"]["forProvider"]["inlinePolicy"][0]["policy"])
def test_flag_on_renders_role_and_association_for_the_proxy():
    docs = render(CHART, *ON)
    assert len(_iam_roles(docs)) == 1
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
# AI backend and per-profile routes (llm-backend.yaml, guardrail-routes.yaml).
import yaml  # noqa: E402
from tests._guardrails_helm import run  # noqa: E402
GATEWAY_HOST = "agentgateway-proxy.agentgateway-system.svc.cluster.local"
def _by_name(docs, kind):
    return {d["metadata"]["name"]: d for d in of_kind(docs, kind)}
def _llm(docs):
    out = []
    for d in docs:
        n = d["metadata"]["name"]
        if n.startswith("llm") and d["kind"] in ("HTTPRoute", "AgentgatewayPolicy", "AgentgatewayBackend"):
            out.append(d)
    return out
def test_flag_off_renders_no_llm_objects():
    assert _llm(render(CHART, *OFF)) == []
def test_flag_on_renders_one_backend_three_routes_three_access_policies():
    docs = render(CHART, *ON)
    llm = _llm(docs)
    assert [b["metadata"]["name"] for b in of_kind(llm, "AgentgatewayBackend")] == ["llm"]
    assert set(_by_name(llm, "HTTPRoute")) == {"llm-observe", "llm-standard", "llm-strict"}
    policies = _by_name(llm, "AgentgatewayPolicy")
    assert {n for n in policies if n.endswith("-access")} == {"llm-observe-access", "llm-standard-access", "llm-strict-access"}
    for d in llm:
        assert d["metadata"]["namespace"] == "agentgateway-system"
def test_backend_is_a_custom_provider_aimed_at_bifrost():
    backend = _by_name(render(CHART, *ON), "AgentgatewayBackend")["llm"]
    provider = backend["spec"]["ai"]["provider"]
    assert "openai" not in provider
    assert provider["host"] == "bifrost.bifrost.svc.cluster.local" and provider["port"] == 8080
    assert provider["custom"]["formats"] == [{"type": "Completions", "path": "/v1/chat/completions"}]
def test_routes_attach_to_the_gateway_and_rewrite_the_prefix():
    routes = _by_name(render(CHART, *ON), "HTTPRoute")
    for profile in ("observe", "standard", "strict"):
        r = routes[f"llm-{profile}"]["spec"]
        assert r["parentRefs"][0]["name"] == "agentgateway-proxy"
        assert r["parentRefs"][0]["namespace"] == "agentgateway-system"
        rule = r["rules"][0]
        assert rule["matches"][0]["path"] == {"type": "PathPrefix", "value": f"/llm/{profile}"}
        assert rule["filters"][0]["urlRewrite"]["path"] == {"type": "ReplacePrefixMatch", "replacePrefixMatch": "/"}
        assert rule["backendRefs"][0]["kind"] == "AgentgatewayBackend"
def test_every_route_matches_only_the_in_cluster_host():
    docs = render(CHART, "--set", "ingress.host=idp.example.test", *ON)
    ingress_hosts = {
        rule["host"]
        for ing in of_kind(docs, "Ingress")
        for rule in ing["spec"]["rules"]
    }
    assert ingress_hosts
    for route in _by_name(docs, "HTTPRoute").values():
        hosts = route["spec"].get("hostnames")
        if route["metadata"]["name"].startswith("llm-"):
            assert hosts == [GATEWAY_HOST]
            assert not (set(hosts) & ingress_hosts)
def _expr(docs, profile):
    p = _by_name(docs, "AgentgatewayPolicy")[f"llm-{profile}-access"]
    auth = p["spec"]["traffic"]["authorization"]
    assert auth["action"] == "Require"
    assert len(auth["policy"]["matchExpressions"]) == 1
    return auth["policy"]["matchExpressions"][0]
def test_profiles_at_or_above_the_default_admit_every_service_account():
    docs = render(CHART, *ON)
    for profile in ("standard", "strict"):
        assert _expr(docs, profile) == 'has(jwt.sub) && jwt.sub.startsWith("system:serviceaccount:")'
def test_weaker_profile_admits_nobody_without_a_grant():
    assert _expr(render(CHART, *ON), "observe") == "jwt.sub in []"
def test_weaker_profile_admits_only_granted_agents():
    docs = render(CHART, *ON, "--set", "guardrailAccess.observe.agents={team-a/agent-1,team-b/agent-2}")
    assert _expr(docs, "observe") == (
        'jwt.sub in ["system:serviceaccount:team-a:agent-1", "system:serviceaccount:team-b:agent-2"]'
    )
    assert _expr(docs, "standard").startswith("has(jwt.sub)")
def test_default_profile_moves_the_open_boundary():
    docs = render(CHART, *ON, "--set", "guardrails.defaultProfile=observe")
    assert _expr(docs, "observe").startswith("has(jwt.sub)")
    docs = render(CHART, *ON, "--set", "guardrails.defaultProfile=strict")
    assert _expr(docs, "standard") == "jwt.sub in []"
def test_bare_agent_name_in_guardrail_access_fails():
    out = run(CHART, *ON, "--set", "guardrailAccess.observe.agents={agent-1}")
    assert out.returncode != 0 and "<namespace>/<agent>" in out.stderr
def test_grant_for_an_open_profile_fails():
    out = run(CHART, *ON, "--set", "guardrailAccess.strict.agents={a/b}")
    assert out.returncode != 0 and "already admits every agent" in out.stderr
def test_grant_for_an_unknown_profile_fails():
    out = run(CHART, *ON, "--set", "guardrailAccess.nope.agents={a/b}")
    assert out.returncode != 0 and "not in guardrails.order" in out.stderr
def test_profile_must_be_in_the_order_and_set_failure_mode_and_scope():
    out = run(CHART, *ON, "--set", "guardrails.profiles.extra.action=Reject")
    assert out.returncode != 0 and "not listed in guardrails.order" in out.stderr
    out = run(CHART, *ON, "--set", "guardrails.profiles.strict.failureMode=")
    assert out.returncode != 0 and "failureMode" in out.stderr
    out = run(CHART, *ON, "--set", "guardrails.profiles.strict.scope=null")
    assert out.returncode != 0 and "scope" in out.stderr
def test_both_charts_name_the_same_profiles_in_the_same_order():
    gw = yaml.safe_load((CHARTS / "agent-gateway/values.yaml").read_text())["guardrails"]
    xp = yaml.safe_load((CHARTS / "crossplane-agentcore/values.yaml").read_text())["guardrails"]
    assert gw["order"] == xp["order"]
    assert set(gw["profiles"]) == set(xp["profiles"]) == set(gw["order"])
def test_flag_off_with_a_bad_catalog_still_renders_nothing():
    docs = render(CHART, *OFF, "--set", "guardrails.profiles.extra.action=Reject")
    assert _llm(docs) == []
# Metrics Service (proxy-metrics-service.yaml).
def _metrics_service(docs):
    return [d for d in of_kind(docs, "Service") if d["metadata"]["name"] == "agentgateway-proxy-metrics"]
def test_flag_off_renders_no_metrics_service():
    assert _metrics_service(render(CHART, *OFF)) == []
def test_flag_on_renders_a_cluster_ip_metrics_service():
    [svc] = _metrics_service(render(CHART, *ON))
    assert svc["metadata"]["namespace"] == "agentgateway-system"
    assert svc["spec"]["type"] == "ClusterIP"
    ann = svc["metadata"]["annotations"]
    assert ann["prometheus.io/scrape"] == "true" and ann["prometheus.io/port"] == "15020"
    assert ann["prometheus.io/path"] == "/metrics"
    assert svc["spec"]["selector"] == {
        "app.kubernetes.io/name": "agentgateway-proxy",
        "app.kubernetes.io/instance": "agentgateway-proxy",
    }
    assert [p["port"] for p in svc["spec"]["ports"]] == [15020]
    assert "nodePort" not in svc["spec"]["ports"][0]
