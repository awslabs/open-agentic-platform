"""The agent component wires the harness through Argo Rollouts analysis.

Rendered through the real definitions (helm template of the oam-agent-components
chart, then `vela dry-run --offline`) for the three shipped examples.
"""
from __future__ import annotations

import copy
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
CHART = ROOT / "gitops/addons/charts/oam-agent-components"
EXAMPLES = ROOT / "platform/oam/examples"
REPORT = EXAMPLES / "example-agent-harness.yaml"
MANUAL = EXAMPLES / "example-agent-harness-manual-gate.yaml"
GATE = EXAMPLES / "example-agent-harness-gate.yaml"

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None or shutil.which("vela") is None,
    reason="helm and vela are needed to render the agent component",
)


@pytest.fixture(scope="module")
def render(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("agent-rollout")
    kubeconfig = tmp / "kubeconfig"
    kubeconfig.write_text(
        "apiVersion: v1\nkind: Config\n"
        'clusters: [{name: none, cluster: {server: "https://127.0.0.1:1"}}]\n'
        "users: [{name: none, user: {token: none}}]\n"
        "contexts: [{name: none, context: {cluster: none, user: none}}]\n"
        "current-context: none\n",
        encoding="utf-8",
    )
    defs = tmp / "defs"
    subprocess.run(
        ["helm", "template", "oap", str(CHART), "--set", "global.clusterName=c1", "--output-dir", str(defs)],
        check=True, capture_output=True,
    )
    env = {**os.environ, "KUBECONFIG": str(kubeconfig)}

    def _render(app: dict) -> dict:
        path = tmp / f"app-{id(app)}.yaml"
        path.write_text(yaml.safe_dump(app), encoding="utf-8")
        out = subprocess.run(
            ["vela", "dry-run", "--offline", "-d", str(defs / "oam-agent-components/templates"), "-f", str(path)],
            check=True, capture_output=True, text=True, env=env,
        ).stdout
        docs = [d for d in yaml.safe_load_all(out) if isinstance(d, dict)]
        by_kind: dict = {}
        for d in docs:
            by_kind.setdefault(d.get("kind"), d)
        by_kind["_all"] = docs
        return by_kind

    def _render_expecting_failure(app: dict) -> subprocess.CompletedProcess:
        path = tmp / f"bad-{id(app)}.yaml"
        path.write_text(yaml.safe_dump(app), encoding="utf-8")
        return subprocess.run(
            ["vela", "dry-run", "--offline", "-d", str(defs / "oam-agent-components/templates"), "-f", str(path)],
            capture_output=True, text=True, env=env,
        )

    _render.fails = _render_expecting_failure
    return _render


def _app(path: Path, **harness) -> dict:
    app = copy.deepcopy(yaml.safe_load(path.read_text(encoding="utf-8")))
    props = app["spec"]["components"][0]["properties"]
    if harness.get("_drop"):
        props.pop("harness", None)
    else:
        props["harness"] = {**props["harness"], **harness}
    return app


def _bg(docs):
    return docs["Rollout"]["spec"]["strategy"]["blueGreen"]


def test_gate_runs_before_promotion_with_no_dry_run_and_no_timer(render):
    docs = render(_app(GATE))
    bg = _bg(docs)
    assert "prePromotionAnalysis" in bg and "postPromotionAnalysis" not in bg
    assert "autoPromotionSeconds" not in bg
    tpl = docs["AnalysisTemplate"]
    assert "dryRun" not in tpl["spec"]
    assert bg["prePromotionAnalysis"]["templates"] == [{"templateName": "harness-assistant-gate-harness"}]
    assert tpl["metadata"]["name"] == "harness-assistant-gate-harness"


def test_gate_aborts_with_no_scale_down_deadline(render):
    # 0, not absent: absent means the 30 s default, which leaves the
    # fast-track promotion path open (rollout/bluegreen.go isBlueGreenFastTracked).
    bg = _bg(render(_app(GATE)))
    assert "abortScaleDownDelaySeconds" in bg and bg["abortScaleDownDelaySeconds"] == 0


@pytest.mark.parametrize("path,harness", [
    (REPORT, {}),
    (MANUAL, {}),
    (REPORT, {"_drop": True}),
    (GATE, {"enabled": False}),
])
def test_only_a_gate_sets_abort_scale_down_delay(render, path, harness):
    assert "abortScaleDownDelaySeconds" not in _bg(render(_app(path, **harness)))


def test_dashcam_report_scores_after_promotion_with_dry_run(render):
    docs = render(_app(REPORT))
    bg = _bg(docs)
    assert "postPromotionAnalysis" in bg and "prePromotionAnalysis" not in bg
    assert bg["autoPromotionSeconds"] == 10
    assert docs["AnalysisTemplate"]["spec"]["dryRun"] == [{"metricName": "oap-harness"}]


def test_manual_gate_report_holds_for_a_person_with_dry_run(render):
    docs = render(_app(MANUAL))
    bg = _bg(docs)
    assert "prePromotionAnalysis" in bg and "postPromotionAnalysis" not in bg
    assert bg["autoPromotionEnabled"] is False
    assert docs["AnalysisTemplate"]["spec"]["dryRun"] == [{"metricName": "oap-harness"}]


def test_template_metric_and_url_use_platform_values_and_argo_placeholders(render):
    tpl = render(_app(GATE))["AnalysisTemplate"]
    metric = tpl["spec"]["metrics"][0]
    assert metric["successCondition"] == "result.state == 'pass'"
    assert "'insufficient'" in metric["failureCondition"] and "'error'" in metric["failureCondition"]
    assert metric["count"] == 160 and metric["inconclusiveLimit"] == 160
    url = metric["provider"]["web"]["url"]
    assert url.startswith("http://agent-evaluation-harness-api.agent-evaluation.svc.cluster.local:8081/v1/verdict?")
    for arg in ("namespace", "agent", "revision", "hash"):
        assert "{{args.%s}}" % arg in url
    assert [a["name"] for a in tpl["spec"]["args"]] == ["agent", "namespace", "revision", "hash"]


def test_rollout_args_resolve_from_the_rollout(render):
    args = {a["name"]: a["valueFrom"] for a in _bg(render(_app(GATE)))["prePromotionAnalysis"]["args"]}
    assert args["hash"] == {"podTemplateHashValue": "Latest"}
    assert args["revision"]["fieldRef"]["fieldPath"] == "metadata.annotations['rollout.argoproj.io/revision']"
    labels = _bg(render(_app(GATE)))["prePromotionAnalysis"]["analysisRunMetadata"]["labels"]
    assert labels == {"harness.oap.io/agent": "harness-assistant-gate"}


def test_agent_without_harness_has_neither_template_nor_analysis(render):
    docs = render(_app(REPORT, _drop=True))
    bg = _bg(docs)
    assert "AnalysisTemplate" not in docs
    assert "prePromotionAnalysis" not in bg and "postPromotionAnalysis" not in bg


def test_disabled_harness_has_neither_template_nor_analysis(render):
    docs = render(_app(GATE, enabled=False))
    assert "AnalysisTemplate" not in docs
    assert "prePromotionAnalysis" not in _bg(docs)


def test_unknown_mode_is_rejected(render):
    assert render.fails(_app(GATE, mode="other")).returncode != 0


def test_the_harness_declaration_carries_the_mode(render):
    docs = render(_app(GATE))
    cm = next(d for d in docs["_all"] if d.get("kind") == "ConfigMap" and d["metadata"]["name"].endswith("-harness"))
    assert cm["data"]["mode"] == "gate"
