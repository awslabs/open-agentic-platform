"""Per-agent guardrail outcomes from the gateway access log (design D8, OPEN-2).

The gateway exports one OTLP log record per /llm request; the OTel collector counts the
records and writes the counters to AMP. These tests pin the gateway policy (an allowlist of
attributes, none carrying content), the collector pipeline (every reference resolves, the
old config is untouched with the flag off) and the registry switches.
"""
from __future__ import annotations
import re
from pathlib import Path
import pytest
import yaml
from tests._guardrails_helm import CHARTS, needs_helm, of_kind, render
from tests.test_guardrails_enablement import ROOT, T, F, N, _entry
from tests.test_harness_enablement import _evaluate
pytestmark = needs_helm
GATEWAY = "agent-gateway"
COLLECTOR = "otel-collector"
GW_ON = ["--set", "global.clusterName=c1", "--set", "global.awsRegion=us-west-2",
         "--set-string", "global.awsAccountId=111122223333", "--set-string", "guardrails.enabled=true"]
ACCESSLOG = ["--set-string", "guardrails.accessLog.enabled=true"]
AMP_LITERAL = ["--set", "amp.enabled=true", "--set", "amp.remoteWriteEndpoint=https://aps.example/ws/"]
AMP_SECRET = ["--set", "amp.enabled=true", "--set", "amp.secretManagerKey=k", "--set", "amp.clusterName=c1"]
ON = ["--set-string", "guardrails.accessLog.enabled=true"]
ALLOWED_ATTRIBUTES = {"agent", "route", "status", "reason", "guardrail", "guardrail_version"}
# Anything that could carry request or response content, or the guard's own findings.
FORBIDDEN = re.compile(
    r"request\.body|response\.body|bodyPrefix|llm\.prompt|llm\.completion|llmRequest|"
    r"assessments|actionReason|headers|toolCalls|rawToken",
)


def _policies(docs):
    return [d for d in of_kind(docs, "AgentgatewayPolicy") if d["metadata"]["name"] == "llm-guardrail-accesslog"]


def test_gateway_flag_off_renders_no_access_log_policy():
    assert _policies(render(GATEWAY)) == []


def test_guardrails_on_without_access_log_renders_no_access_log_policy():
    assert _policies(render(GATEWAY, *GW_ON)) == []


def test_access_log_alone_renders_nothing_without_guardrails():
    assert _policies(render(GATEWAY, *ACCESSLOG)) == []


def test_policy_targets_the_gateway_and_exports_over_otlp_http_to_the_collector():
    (policy,) = _policies(render(GATEWAY, *GW_ON, *ACCESSLOG))
    spec = policy["spec"]
    assert policy["metadata"]["namespace"] == "agentgateway-system"
    assert spec["targetRefs"] == [
        {"group": "gateway.networking.k8s.io", "kind": "Gateway", "name": "agentgateway-proxy"},
    ]
    assert set(spec) == {"targetRefs", "frontend"}
    otlp = spec["frontend"]["accessLog"]["otlp"]
    assert otlp["protocol"] == "HTTP"
    assert otlp["url"] == "http://otel-collector-guardrail.otel.svc.cluster.local:4318"


def test_export_is_limited_to_the_llm_routes_and_leaves_other_access_logs_alone():
    (policy,) = _policies(render(GATEWAY, *GW_ON, *ACCESSLOG))
    access_log = policy["spec"]["frontend"]["accessLog"]
    # Filter and attributes sit under otlp only: a filter on accessLog itself would also
    # drop the MCP and A2A routes from the default log.
    assert set(access_log) == {"otlp"}
    # Both the bare route name and a namespace/name form are accepted.
    assert 'startsWith("llm-")' in access_log["otlp"]["filter"]
    assert 'contains("/llm-")' in access_log["otlp"]["filter"]


def test_attributes_are_exactly_the_allowlist_and_none_carries_content():
    (policy,) = _policies(render(GATEWAY, *GW_ON, *ACCESSLOG))
    added = policy["spec"]["frontend"]["accessLog"]["otlp"]["attributes"]["add"]
    assert {a["name"] for a in added} == ALLOWED_ATTRIBUTES
    assert "remove" not in policy["spec"]["frontend"]["accessLog"]["otlp"]["attributes"]
    for attr in added:
        assert not FORBIDDEN.search(attr["expression"]), attr
    expressions = {a["name"]: a["expression"] for a in added}
    assert expressions["agent"] == "jwt.sub"
    assert expressions["route"] == "proxy.route.name"


def test_template_has_no_literal_namespace_or_host():
    text = (CHARTS / "agent-gateway/templates/guardrail-accesslog.yaml").read_text(encoding="utf-8")
    body = re.sub(r"\{\{/\*.*?\*/\}\}", "", text, flags=re.S)
    assert "otel-collector" not in body and "svc.cluster.local" not in body
    assert not re.search(r"\b\d{12}\b", text)


# ---- collector chart ------------------------------------------------------------------

def _config(docs, name):
    cm = next(d for d in of_kind(docs, "ConfigMap") if d["metadata"]["name"] == name)
    return yaml.safe_load(cm["data"]["config.yaml"])


def _deployment(docs, name):
    return next(d for d in of_kind(docs, "Deployment") if d["metadata"]["name"] == name)


def _image(docs, name):
    return _deployment(docs, name)["spec"]["template"]["spec"]["containers"][0]["image"]


def _check_references(config):
    """Every component a pipeline names is defined, and every connector joins two pipelines."""
    service = config["service"]
    defined = {
        "receivers": set(config.get("receivers") or {}),
        "processors": set(config.get("processors") or {}),
        "exporters": set(config.get("exporters") or {}),
        "connectors": set(config.get("connectors") or {}),
    }
    for name, pipe in service["pipelines"].items():
        for rcv in pipe.get("receivers", []):
            assert rcv in defined["receivers"] | defined["connectors"], (name, rcv)
        for proc in pipe.get("processors", []):
            assert proc in defined["processors"], (name, proc)
        for exp in (pipe.get("exporters") or []):
            assert exp in defined["exporters"] | defined["connectors"], (name, exp)
    for ext in service["extensions"]:
        assert ext in (config.get("extensions") or {}), ext
    for conn in defined["connectors"]:
        as_exporter = [n for n, p in service["pipelines"].items() if conn in (p.get("exporters") or [])]
        as_receiver = [n for n, p in service["pipelines"].items() if conn in p.get("receivers", [])]
        assert as_exporter and as_receiver, conn


@pytest.mark.parametrize("amp", [AMP_LITERAL, AMP_SECRET, []], ids=["literal", "secret", "no-amp"])
def test_collector_flag_off_is_the_same_as_the_default(amp):
    assert render(COLLECTOR, *amp) == render(COLLECTOR, "--set-string", "guardrails.accessLog.enabled=false", *amp)


@pytest.mark.parametrize("amp", [AMP_LITERAL, AMP_SECRET, []], ids=["literal", "secret", "no-amp"])
def test_collector_flag_off_has_no_pipeline_connector_service_or_new_image(amp):
    docs = render(COLLECTOR, *amp)
    for name in ("otel-collector-config", "otel-collector-metrics-config"):
        cm = [d for d in of_kind(docs, "ConfigMap") if d["metadata"]["name"] == name]
        for d in cm:
            config = yaml.safe_load(d["data"]["config.yaml"])
            assert "connectors" not in config
            assert not [p for p in config["service"]["pipelines"] if "guardrail" in p]
    assert [d for d in of_kind(docs, "Service") if "guardrail" in d["metadata"]["name"]] == []
    assert "contrib" not in _image(docs, "otel-collector")


@pytest.mark.parametrize("amp", [AMP_LITERAL, AMP_SECRET, []], ids=["literal", "secret", "no-amp"])
def test_collector_flag_on_renders_a_valid_config_in_every_deployment(amp):
    docs = render(COLLECTOR, *amp, *ON)
    for name in ("otel-collector-config", "otel-collector-metrics-config"):
        for d in [d for d in of_kind(docs, "ConfigMap") if d["metadata"]["name"] == name]:
            _check_references(yaml.safe_load(d["data"]["config.yaml"]))


def _host(docs):
    """The config that holds the guardrail pipelines."""
    hosts = []
    for d in of_kind(docs, "ConfigMap"):
        if d["metadata"]["name"] in ("otel-collector-config", "otel-collector-metrics-config"):
            config = yaml.safe_load(d["data"]["config.yaml"])
            if "logs/guardrail" in config["service"]["pipelines"]:
                hosts.append((d["metadata"]["name"], config))
    assert len(hosts) == 1, [h[0] for h in hosts]
    return hosts[0]


def test_literal_amp_writes_from_the_main_collector():
    docs = render(COLLECTOR, *AMP_LITERAL, *ON)
    name, config = _host(docs)
    assert name == "otel-collector-config"
    pipes = config["service"]["pipelines"]
    assert pipes["metrics/guardrail"]["exporters"] == ["prometheusremotewrite/amp"]
    assert pipes["logs/guardrail"]["receivers"] == ["otlp"]
    assert "contrib" in _image(docs, "otel-collector")
    svc = next(d for d in of_kind(docs, "Service") if d["metadata"]["name"] == "otel-collector-guardrail")
    assert svc["spec"]["selector"] == {"app": "otel-collector"}
    assert [p["port"] for p in svc["spec"]["ports"]] == [4318]
    assert svc["spec"]["type"] == "ClusterIP"


def test_secret_amp_writes_from_the_metrics_collector_and_leaves_the_traces_collector_alone():
    docs = render(COLLECTOR, *AMP_SECRET, *ON)
    name, config = _host(docs)
    assert name == "otel-collector-metrics-config"
    assert config["service"]["pipelines"]["metrics/guardrail"]["exporters"] == ["prometheusremotewrite/amp"]
    assert "contrib" in _image(docs, "otel-collector-metrics")
    assert "contrib" not in _image(docs, "otel-collector")
    svc = next(d for d in of_kind(docs, "Service") if d["metadata"]["name"] == "otel-collector-guardrail")
    assert svc["spec"]["selector"] == {"app": "otel-collector-metrics"}
    # One writer: the main collector has no guardrail pipeline.
    main = _config(docs, "otel-collector-config")
    assert "logs/guardrail" not in main["service"]["pipelines"]


def test_without_amp_the_counters_go_to_the_debug_exporter():
    docs = render(COLLECTOR, *ON)
    _, config = _host(docs)
    assert config["service"]["pipelines"]["metrics/guardrail"]["exporters"] == ["debug/guardrail"]


def test_counters_are_cumulative_and_idle_streams_expire():
    docs = render(COLLECTOR, *AMP_LITERAL, *ON)
    _, config = _host(docs)
    assert config["service"]["pipelines"]["metrics/guardrail"]["processors"][0] == "deltatocumulative/guardrail"
    assert config["processors"]["deltatocumulative/guardrail"] == {"max_stale": "1h"}


def test_request_counter_carries_the_attribute_set_the_rules_read_and_versions_have_no_agent():
    docs = render(COLLECTOR, *AMP_LITERAL, *ON)
    _, config = _host(docs)
    logs = config["connectors"]["count/guardrail"]["logs"]
    requests = logs["oap.guardrail.accesslog.requests"]["attributes"]
    versions = logs["oap.guardrail.accesslog.versions"]["attributes"]
    assert [a["key"] for a in requests] == ["agent", "route", "status", "reason", "guardrail"]
    assert {a["key"]: a["default_value"] for a in requests if "default_value" in a} == {
        "reason": "none", "guardrail": "none"}
    # A version never multiplies the per-agent series (design D8).
    assert [a["key"] for a in versions] == ["route", "guardrail_version"]
    assert {a["key"] for a in requests + versions} <= ALLOWED_ATTRIBUTES


def test_the_new_image_is_a_pinned_value_not_latest():
    values = yaml.safe_load((CHARTS / "otel-collector/values.yaml").read_text())
    image = values["guardrails"]["accessLog"]["image"]
    assert re.search(r":\d+\.\d+\.\d+$", image) and "latest" not in image
    assert values["guardrails"]["accessLog"]["enabled"] == "false"


# ---- registry switches ----------------------------------------------------------------

LABELS = ("enable_agent_guardrails", "enable_agent_gateway", "enable_agent_core", "enable_otel_collector")
# (guardrails, gateway, core, otel) -> (gateway exports, collector pipeline)
CASES = [
    (N, N, N, N, False, False),
    (T, T, T, N, False, True),   # no collector label: the collector addon is not deployed anyway
    (T, T, T, T, True, True),
    (T, T, N, T, False, False),
    (T, N, T, T, False, False),
    (F, T, T, T, False, False),
]


@pytest.mark.parametrize("guardrails, gateway, core, otel, exports, pipeline", CASES)
def test_registry_switches_follow_the_labels(tmp_path, guardrails, gateway, core, otel, exports, pipeline):
    labels = dict(zip(LABELS, (guardrails, gateway, core, otel)))
    gw = _entry("gateway.yaml", "agent-gateway")["guardrails"]["accessLog"]["enabled"]
    col = _entry("observability.yaml", "otel-collector")["guardrails"]["accessLog"]["enabled"]
    assert _evaluate(gw, labels, tmp_path / "gw") == ("true" if exports else "false")
    assert _evaluate(col, labels, tmp_path / "col") == ("true" if pipeline else "false")


def test_collector_service_name_and_namespace_match_between_the_two_charts():
    gw = yaml.safe_load((CHARTS / "agent-gateway/values.yaml").read_text())["guardrails"]["accessLog"]["collector"]
    col = yaml.safe_load((CHARTS / "otel-collector/values.yaml").read_text())["guardrails"]["accessLog"]
    reg = yaml.safe_load((ROOT / "gitops/addons/registry/observability.yaml").read_text())["otel-collector"]
    assert gw["name"] == col["serviceName"]
    assert gw["namespace"] == reg["namespace"]
    assert gw["port"] == 4318
