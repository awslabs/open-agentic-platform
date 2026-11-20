"""One cluster-secret label turns the harness on in both charts.

agent-evaluation's harness.enabled and oam-agent-components'
global.agentEvaluation.harnessEnabled are both set by their registry entries
from the enable_agent_evaluation and enable_agent_harness labels. If they could
disagree, a gate would render on a cluster with no verdict endpoint and every
revision would abort. This test evaluates the two registry expressions for each
label combination (with helm's template engine, the same Go text/template and
sprig the ApplicationSet controller uses), then renders both charts with the
results.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.test_agent_harness_rollout import GATE, _app, _renderer

ROOT = Path(__file__).resolve().parents[3]
REGISTRY = ROOT / "gitops/addons/registry"
EVAL_CHART = ROOT / "gitops/addons/charts/agent-evaluation"
OAM_CHART = ROOT / "gitops/addons/charts/oam-agent-components"
ENVIRONMENTS = ROOT / "gitops/overlays/environments"

needs_helm = pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")

# (enable_agent_evaluation, enable_agent_harness) -> harness on. None is an
# absent label: the fleet-secret chart writes enable_<addon>: 'true' only for a
# true flag and nothing for false. "false" is a hand-set label.
CASES = [
    (None, None, False),
    ("true", None, False),
    (None, "true", False),  # the flag alone implies nothing
    ("true", "false", False),
    ("true", "true", True),
]


def _eval_expression() -> str:
    entry = yaml.safe_load((REGISTRY / "observability.yaml").read_text(encoding="utf-8"))
    return entry["agent-evaluation"]["valuesObject"]["harness"]["enabled"]


def _oam_expression() -> str:
    entry = yaml.safe_load((REGISTRY / "agentcore.yaml").read_text(encoding="utf-8"))
    return entry["oam-agent-components"]["valuesObject"]["global"]["agentEvaluation"]["harnessEnabled"]


def _evaluate(expression: str, labels: dict, tmp: Path) -> str:
    """Evaluate a registry template with .metadata.labels set to labels.

    ArgoCD types labels as map[string]string, where `index` on an absent key
    returns "". A helm values map would return nil, so an absent label is
    passed as "" to keep the same semantics.
    """
    chart = tmp / "expr"
    (chart / "templates").mkdir(parents=True, exist_ok=True)
    (chart / "Chart.yaml").write_text("apiVersion: v2\nname: expr\nversion: 0.0.0\n", encoding="utf-8")
    (chart / "templates/out.yaml").write_text(
        "{{- with .Values.cluster }}value: '" + expression + "'{{ end }}\n", encoding="utf-8"
    )
    values = tmp / "labels.yaml"
    full = {k: (v if v is not None else "") for k, v in labels.items()}
    values.write_text(yaml.safe_dump({"cluster": {"metadata": {"labels": full}}}), encoding="utf-8")
    out = subprocess.run(
        ["helm", "template", "e", str(chart), "-f", str(values)],
        check=True, capture_output=True, text=True,
    ).stdout
    return next(d for d in yaml.safe_load_all(out) if d)["value"]


def _eval_renders_harness(value: str) -> bool:
    out = subprocess.run(
        ["helm", "template", "t", str(EVAL_CHART),
         "--set", "global.clusterName=c1", "--set", "langfuse.url=http://x",
         "--set", "langfuse.secretManagerKey=p-hub/langfuse-otel",
         "--set-string", f"harness.enabled={value}"],
        check=True, capture_output=True, text=True,
    ).stdout
    docs = [d for d in yaml.safe_load_all(out) if d]
    return any(d["kind"] == "Service" and d["metadata"]["name"] == "agent-evaluation-harness-api" for d in docs)


def test_both_registry_entries_use_one_expression():
    assert _eval_expression() == _oam_expression()
    assert "enable_agent_harness" in _eval_expression()
    assert "enable_agent_evaluation" in _eval_expression()


@pytest.mark.parametrize("env", ["control-plane", "dev", "prod"])
def test_harness_flag_is_off_in_every_environment(env):
    addons = yaml.safe_load((ENVIRONMENTS / env / "enabled-addons.yaml").read_text(encoding="utf-8"))
    assert addons["enabledAddons"]["agent_harness"] is False


def test_chart_defaults_are_off():
    assert yaml.safe_load((EVAL_CHART / "values.yaml").read_text())["harness"]["enabled"] is False
    oam = yaml.safe_load((OAM_CHART / "values.yaml").read_text())
    assert oam["global"]["agentEvaluation"]["harnessEnabled"] == "false"


@needs_helm
@pytest.mark.parametrize("evaluation, harness, on", CASES)
def test_both_switches_follow_the_labels_and_agree(tmp_path, evaluation, harness, on):
    labels = {"enable_agent_evaluation": evaluation, "enable_agent_harness": harness}
    eval_value = _evaluate(_eval_expression(), labels, tmp_path / "a")
    oam_value = _evaluate(_oam_expression(), labels, tmp_path / "b")
    assert eval_value == oam_value == ("true" if on else "false")

    # The agent-evaluation chart reads the registry's string, not a bool.
    assert _eval_renders_harness(eval_value) is on
    oam = subprocess.run(
        ["helm", "template", "oap", str(OAM_CHART), "--set", "global.clusterName=c1",
         "--set-string", f"global.agentEvaluation.harnessEnabled={oam_value}",
         "--show-only", "templates/agent.yaml"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert f'_harnessPlatform: "{oam_value}"' in oam


@needs_helm
@pytest.mark.skipif(shutil.which("vela") is None, reason="vela is not installed")
@pytest.mark.parametrize("evaluation, harness, on", [CASES[2], CASES[4]])
def test_gate_renders_only_where_the_endpoint_renders(tmp_path, evaluation, harness, on):
    labels = {"enable_agent_evaluation": evaluation, "enable_agent_harness": harness}
    value = _evaluate(_oam_expression(), labels, tmp_path / "x")
    render = _renderer(tmp_path / "r", "--set-string", f"global.agentEvaluation.harnessEnabled={value}")
    result = render.fails(_app(GATE))
    assert (result.returncode == 0) is on
    if not on:
        assert "_harnessPlatformRequired" in result.stderr + result.stdout
        assert "agent_harness: true" in result.stderr + result.stdout
    assert _eval_renders_harness(_evaluate(_eval_expression(), labels, tmp_path / "y")) is on


@needs_helm
def test_eval_chart_rejects_an_unknown_harness_value():
    r = subprocess.run(
        ["helm", "template", "t", str(EVAL_CHART),
         "--set", "global.clusterName=c1", "--set", "langfuse.url=http://x",
         "--set", "langfuse.secretManagerKey=p-hub/langfuse-otel",
         "--set-string", "harness.enabled=yes"],
        capture_output=True, text=True,
    )
    assert r.returncode != 0
    assert "harness.enabled must be true or false" in r.stderr
