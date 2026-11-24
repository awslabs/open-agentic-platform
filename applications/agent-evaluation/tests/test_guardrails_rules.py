"""Guardrail recording rules, alerts and info series (design D8).

The PrometheusRule is rendered with helm, its groups are written to a file and promtool checks
and unit-tests them (tests/fixtures/guardrail-rules-test.yaml). The promtool tests skip when
promtool is not installed; the render tests do not.
"""
from __future__ import annotations
import re
import shutil
import subprocess
from pathlib import Path
import pytest
import yaml
from tests._guardrails_helm import CHARTS, needs_helm, of_kind, render, run
pytestmark = needs_helm
CHART = "agent-gateway"
CRD = ["--api-versions", "monitoring.coreos.com/v1/PrometheusRule"]
BASE = ["--set", "global.clusterName=c1", "--set", "global.awsRegion=us-west-2",
        "--set-string", "global.awsAccountId=111122223333"]
ON = BASE + ["--set-string", "guardrails.enabled=true"]
FIXTURE = Path(__file__).parent / "fixtures/guardrail-rules-test.yaml"
needs_promtool = pytest.mark.skipif(shutil.which("promtool") is None, reason="promtool is not installed")
FAMILY = [
    "oap_agent_guardrail_requests_total",
    "oap_agent_guardrail_findings_total",
    "oap_agent_guardrail_latency_seconds_bucket",
    "oap_agent_guardrail_latency_seconds_sum",
    "oap_agent_guardrail_latency_seconds_count",
    "oap_agent_guardrail_errors_total",
    "oap_agent_guardrail_info",
    "oap_agent_guardrail_config_skew",
    "oap_agent_guardrail_last_success_timestamp_seconds",
]


def _rule(docs):
    return of_kind(docs, "PrometheusRule")


def _rules(rule):
    return [r for g in rule["spec"]["groups"] for r in g["rules"]]


def test_flag_off_renders_no_rule_even_with_the_crd():
    assert _rule(render(CHART, *BASE, *CRD)) == []


def test_without_the_crd_nothing_renders_and_the_chart_still_renders():
    docs = render(CHART, *ON)
    assert _rule(docs) == []
    assert of_kind(docs, "HTTPRoute")


def test_alerts_off_renders_no_rule():
    assert _rule(render(CHART, *ON, *CRD, "--set", "guardrails.alerts.enabled=false")) == []


def test_with_the_crd_one_rule_holds_the_whole_family():
    (rule,) = _rule(render(CHART, *ON, *CRD))
    names = {r.get("record") for r in _rules(rule)}
    assert set(FAMILY) <= names
    assert [g["name"] for g in rule["spec"]["groups"]] == [
        "oap-agent-guardrail", "oap-agent-guardrail-info", "oap-agent-guardrail-alerts"]


def test_the_recording_rules_are_the_chart_file_unchanged():
    (rule,) = _rule(render(CHART, *ON, *CRD))
    source = yaml.safe_load((CHARTS / "agent-gateway/files/guardrail-rules.yaml").read_text())
    assert rule["spec"]["groups"][0] == source["groups"][0]


def test_every_family_series_is_on_the_model_hop():
    (rule,) = _rule(render(CHART, *ON, *CRD))
    for r in _rules(rule):
        if r.get("record") in FAMILY:
            # Set to "model" here, or carried from another family series that sets it.
            derived = any(name in r["expr"] for name in FAMILY if name != r["record"])
            assert "hop" in r["expr"] and ('"model"' in r["expr"] or derived), r["record"]


def test_alerts_are_the_four_platform_signals_and_none_is_on_a_blocked_rate():
    (rule,) = _rule(render(CHART, *ON, *CRD))
    alerts = {r["alert"]: r for r in _rules(rule) if "alert" in r}
    assert set(alerts) == {"GuardrailFailClosedRefusals", "GuardrailFailOpenFired",
                           "GuardrailDataStale", "GuardrailConfigSkew"}
    for a in alerts.values():
        assert 'outcome="blocked"' not in a["expr"]


def test_no_content_label_or_field_in_the_rules():
    text = (CHARTS / "agent-gateway/files/guardrail-rules.yaml").read_text()
    for banned in ("category", "severity=", "assessments", "prompt", "body"):
        assert not re.search(rf"\b{banned}", re.sub(r"(?m)^\s*#.*$", "", text)), banned


def test_deliberate_fail_open_profiles_alert_at_info_and_unknown_ones_fail():
    (rule,) = _rule(render(CHART, *ON, *CRD, "--set", "guardrails.alerts.deliberateFailOpen={observe,strict}"))
    expr = next(r["expr"] for r in _rules(rule) if r.get("alert") == "GuardrailFailOpenFired")
    rules = [r for r in _rules(rule) if r.get("alert") == "GuardrailFailOpenFired"]
    by_sev = {r["labels"]["severity"]: r["expr"] for r in rules}
    assert 'profile!~"^(observe|strict)$"' in by_sev["warning"]
    assert 'profile=~"^(observe|strict)$"' in by_sev["info"]
    assert "offset 10m" in by_sev["warning"]
    (plain,) = _rule(render(CHART, *ON, *CRD))
    plain_rules = [r for r in _rules(plain) if r.get("alert") == "GuardrailFailOpenFired"]
    assert len(plain_rules) == 1 and "profile" not in plain_rules[0]["expr"].replace("by (cluster, namespace, agent, profile)", "")
    bad = run(CHART, *ON, *CRD, "--set", "guardrails.alerts.deliberateFailOpen={nope}")
    assert bad.returncode != 0 and "nope" in bad.stderr


def test_info_constants_follow_the_profile_values():
    (rule,) = _rule(render(CHART, *ON, *CRD, "--set", "guardrails.profiles.observe.failureMode=FailOpen"))
    expr = next(r["expr"] for r in _rules(rule) if r.get("record") == "oap_agent_guardrail_info")
    assert '"failure_mode", "FailOpen"' in expr
    assert expr.count('"failure_mode"') == 3


def test_catalog_revision_matches_the_provider_chart():
    gw = yaml.safe_load((CHARTS / "agent-gateway/values.yaml").read_text())["guardrails"]
    xp = yaml.safe_load((CHARTS / "crossplane-agentcore/values.yaml").read_text())["guardrails"]
    assert gw["observability"]["info"]["catalogRevision"] == xp["catalogRevision"]


def test_template_has_no_literal_account_region_or_cluster():
    text = (CHARTS / "agent-gateway/templates/guardrail-prometheusrule.yaml").read_text()
    assert not re.search(r"\b\d{12}\b", text)
    assert not re.search(r"\b(us|eu|ap)-[a-z]+-\d\b", text)


def _write_rules(tmp_path, *extra):
    (rule,) = _rule(render(CHART, *ON, *CRD, *extra))
    path = tmp_path / "rules.yaml"
    path.write_text(yaml.safe_dump({"groups": rule["spec"]["groups"]}), encoding="utf-8")
    return path


@needs_promtool
def test_promtool_accepts_the_rules(tmp_path):
    path = _write_rules(tmp_path)
    out = subprocess.run(["promtool", "check", "rules", str(path)], capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr


@needs_promtool
def test_promtool_unit_tests_pass(tmp_path):
    _write_rules(tmp_path, "--set", "guardrails.alerts.deliberateFailOpen={observe}")
    (tmp_path / "t.yaml").write_text(FIXTURE.read_text(), encoding="utf-8")
    out = subprocess.run(["promtool", "test", "rules", str(tmp_path / "t.yaml")],
                         capture_output=True, text=True, cwd=tmp_path)
    assert out.returncode == 0, out.stdout + out.stderr


# ---- dashboard ----------------------------------------------------------------------------

import json  # noqa: E402

GRAFANA = ["--api-versions", "grafana.integreatly.org/v1beta1/GrafanaDashboard"]
DASHBOARD_SRC = CHARTS.parents[2] / "docs/architecture/diagrams/src/agent-guardrails-dashboard.json"
DASHBOARD_COPY = CHARTS / "agent-gateway/files/guardrails-dashboard.json"


def _dashboards(docs):
    return of_kind(docs, "GrafanaDashboard")


def test_dashboard_flag_off_renders_nothing_even_with_the_crd():
    assert _dashboards(render(CHART, *BASE, *GRAFANA)) == []


def test_dashboard_without_the_crd_renders_nothing_and_the_chart_still_renders():
    docs = render(CHART, *ON)
    assert _dashboards(docs) == [] and of_kind(docs, "HTTPRoute")


def test_dashboard_can_be_turned_off_where_the_crd_exists():
    assert _dashboards(render(CHART, *ON, *GRAFANA, "--set", "guardrails.grafana.enabled=false")) == []


def test_dashboard_with_the_crd_renders_one_object_with_parseable_json():
    (dash,) = _dashboards(render(CHART, *ON, *GRAFANA))
    parsed = json.loads(dash["spec"]["json"])
    assert parsed["uid"] == "agent-guardrails"
    assert parsed == json.loads(DASHBOARD_SRC.read_text())
    assert dash["spec"]["instanceSelector"]["matchLabels"] == {"dashboards": "external-grafana"}


def test_the_chart_copy_equals_the_source_dashboard():
    assert DASHBOARD_COPY.read_bytes() == DASHBOARD_SRC.read_bytes()


def test_dashboard_panels_cover_the_design_views_and_use_only_the_family():
    parsed = json.loads(DASHBOARD_SRC.read_text())
    titles = [p["title"] for p in parsed["panels"]]
    for needed in ("Outcomes by profile", "Blocked by agent", "error_blocked",
                   "Request latency p50 and p99", "Configuration in use"):
        assert needed in titles
    exprs = " ".join(t["expr"] for p in parsed["panels"] for t in p.get("targets", []))
    assert set(re.findall(r"\b(oap_[a-z_]+)", exprs)) <= set(FAMILY)
    assert "guardrail=" not in exprs and "agentgateway_" not in exprs
