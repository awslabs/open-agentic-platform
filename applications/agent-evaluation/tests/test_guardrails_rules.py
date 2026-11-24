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


def test_deliberate_fail_open_profiles_are_excluded_and_unknown_ones_fail():
    (rule,) = _rule(render(CHART, *ON, *CRD, "--set", "guardrails.alerts.deliberateFailOpen={observe,strict}"))
    expr = next(r["expr"] for r in _rules(rule) if r.get("alert") == "GuardrailFailOpenFired")
    assert 'profile!~"^(observe|strict)$"' in expr
    (plain,) = _rule(render(CHART, *ON, *CRD))
    assert "profile!~" not in next(r["expr"] for r in _rules(plain) if r.get("alert") == "GuardrailFailOpenFired")
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
