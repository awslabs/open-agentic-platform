"""The harness files the chart ships load through the reconciler's real loaders.

A dataset or check prompt is only edited in the chart, never next to the code
that reads it, so a typo there would otherwise surface only as a failed run on
a cluster. Same idea as feeding the rendered evaluator catalog through
load_catalog.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from app.datasets import load_dataset
from app.harness_config import KNOWN_CHECKS, load_checks
from app.judge import render_prompt

CHART = Path(__file__).resolve().parents[3] / "gitops/addons/charts/agent-evaluation"
DATASETS = sorted((CHART / "files/harness/datasets").glob("*.yaml"))
CHECKS_DIR = CHART / "files/harness/checks"
EXAMPLE_APP = Path(__file__).resolve().parents[3] / "platform/oam/examples/example-agent-harness.yaml"
# The chart's harness.maxCases default.
MAX_CASES = 50


def test_the_chart_ships_at_least_one_dataset():
    assert DATASETS, f"no dataset under {CHART}/files/harness/datasets"


@pytest.mark.parametrize("path", DATASETS, ids=lambda p: p.stem)
def test_every_shipped_dataset_loads(path):
    dataset = load_dataset(path, max_cases=MAX_CASES)
    assert dataset.name == path.stem
    assert dataset.cases


def test_general_assistant_v1_has_at_least_20_cases():
    dataset = load_dataset(CHART / "files/harness/datasets/general-assistant-v1.yaml", MAX_CASES)
    assert len(dataset.cases) >= 20


@pytest.mark.parametrize("path", DATASETS, ids=lambda p: p.stem)
def test_every_time_case_is_judged_against_the_clock(path):
    # A time case with a fixed reference fails a correct answer and passes an
    # invented one; {{now}} is rendered at judge time.
    for case in load_dataset(path, max_cases=MAX_CASES).cases:
        if "time" in case.tags:
            assert any(
                token in case.expected
                for token in ("{{now}}", "{{asked}}", "{{date}}", "{{weekday}}")
            ), case.id


def test_the_example_application_names_a_shipped_dataset():
    app = yaml.safe_load(EXAMPLE_APP.read_text(encoding="utf-8"))
    harness = app["spec"]["components"][0]["properties"]["harness"]
    assert harness["dataset"] == "general-assistant-v1"
    assert harness["dataset"] in {p.stem for p in DATASETS}


def test_expected_answer_prompt_has_every_placeholder_and_renders():
    template = (CHECKS_DIR / "expected-answer.md").read_text(encoding="utf-8")
    for placeholder in ("{{input}}", "{{expected}}", "{{output}}", "{{nonce}}"):
        assert placeholder in template
    rendered = render_prompt(template, "IN-MARK", "EXP-MARK", "OUT-MARK", "NONCE-MARK", "NOW-MARK")
    assert "{{" not in rendered
    for marker in ("IN-MARK", "EXP-MARK", "OUT-MARK"):
        assert marker in rendered
    # Every BEGIN/END marker carries the nonce, so data cannot close a fence.
    for line in rendered.splitlines():
        if line.startswith(("BEGIN ", "END ")):
            assert line.endswith(" NONCE-MARK"), line


def test_prompt_does_not_excuse_a_format_the_reference_forbids():
    # Review round 2: "a different format is fine" contradicted the json-only and
    # sentence-count cases.
    template = (CHECKS_DIR / "expected-answer.md").read_text(encoding="utf-8")
    assert "A paraphrase, a different format" not in template
    assert "requires a" in template and "format" in template


def test_no_credentials_case_rewards_a_refusal():
    cases = {c.id: c for c in load_dataset(
        CHART / "files/harness/datasets/general-assistant-v1.yaml", max_cases=MAX_CASES
    ).cases}
    expected = cases["no-credentials"].expected
    assert "Declines" in expected and "refuses" in expected


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_rendered_checks_configmap_loads_through_load_checks(tmp_path):
    out = subprocess.run(
        [
            "helm", "template", "t", str(CHART),
            "--set", "global.clusterName=c1",
            "--set", "langfuse.url=http://x",
            "--set", "langfuse.secretManagerKey=p-hub/langfuse-otel",
            "--set", "harness.enabled=true",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    docs = [d for d in yaml.safe_load_all(out) if d]
    catalog = next(
        d for d in docs
        if d.get("kind") == "ConfigMap"
        and d["metadata"]["name"] == "agent-evaluation-harness-catalog"
    )
    path = tmp_path / "checks.yaml"
    path.write_text(catalog["data"]["checks.yaml"], encoding="utf-8")
    checks = load_checks(str(path))
    assert set(checks) == set(KNOWN_CHECKS)
    assert checks["expected-answer"].case_pass_score == 0.7
    assert "{{expected}}" in checks["expected-answer"].prompt

    datasets_cm = next(
        d for d in docs
        if d.get("kind") == "ConfigMap"
        and d["metadata"]["name"] == "agent-evaluation-harness-datasets"
    )
    assert sorted(datasets_cm["data"]) == sorted(p.name for p in DATASETS)
    for key, body in datasets_cm["data"].items():
        file = tmp_path / key
        file.write_text(body, encoding="utf-8")
        load_dataset(file, max_cases=MAX_CASES)
@pytest.mark.parametrize("path", DATASETS, ids=lambda p: p.stem)
def test_no_action_cases_ask_about_capability_not_ask_for_the_action(path):
    # An action input ("Book me a flight") would be carried out by an agent
    # with a booking tool, or a supervisor that delegates, on every run.
    for case in load_dataset(path, max_cases=MAX_CASES).cases:
        if "no-action" in case.tags:
            assert case.input.lower().startswith("can you ") and case.input.endswith("?"), case.id


EVAL_CHART_ARGS = [
    "--set", "global.clusterName=c1", "--set", "langfuse.url=http://x",
    "--set", "langfuse.secretManagerKey=p-hub/langfuse-otel",
]


def _render_eval(*extra):
    out = subprocess.run(
        ["helm", "template", "t", str(CHART), *EVAL_CHART_ARGS, *extra],
        check=True, capture_output=True, text=True,
    ).stdout
    return [d for d in yaml.safe_load_all(out) if d]


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_minter_role_is_limited_to_the_judge_key_secret():
    docs = _render_eval("--set", "harness.enabled=true")
    role = next(d for d in docs if d["kind"] == "Role" and d["metadata"]["name"] == "agent-evaluation-judge-key-minter")
    scoped = [r for r in role["rules"] if r["resources"] == ["secrets"] and "get" in r["verbs"]]
    assert scoped[0]["resourceNames"] == ["agent-evaluation-harness-judge-key"]
    assert [r["verbs"] for r in role["rules"] if r["resources"] == ["secrets"] and "resourceNames" not in r] == [["create"]]
    binding = next(d for d in docs if d["kind"] == "RoleBinding" and d["metadata"]["name"] == "agent-evaluation-judge-key-minter")
    assert binding["subjects"][0]["namespace"] == "bifrost"
    assert binding["subjects"][0]["name"] == "bifrost-vk-minter"


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_minter_role_absent_when_harness_off_and_mint_default_false():
    assert not [d for d in _render_eval() if "judge-key-minter" in d["metadata"]["name"]]
    values = yaml.safe_load((CHART / "values.yaml").read_text())
    assert values["harness"]["judge"]["virtualKey"]["mint"] is False


def _fails(*extra):
    r = subprocess.run(
        ["helm", "template", "t", str(CHART), *EVAL_CHART_ARGS, "--set", "harness.enabled=true", *extra],
        capture_output=True, text=True,
    )
    return r


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_reconciler_role_cannot_write_secrets_and_reads_one():
    docs = _render_eval("--set", "harness.enabled=true")
    role = next(d for d in docs if d["kind"] == "Role" and d["metadata"]["name"] == "agent-evaluation-harness")
    secret_rules = [r for r in role["rules"] if "secrets" in r["resources"]]
    assert secret_rules == [
        {"apiGroups": [""], "resources": ["secrets"], "resourceNames": ["agent-evaluation-harness-judge-key"], "verbs": ["get"]}
    ]
    jobs = next(r for r in role["rules"] if "jobs" in r["resources"])
    assert "patch" in jobs["verbs"] and "create" in jobs["verbs"]


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_cluster_role_reads_analysis_runs_and_gets_rollouts_only():
    docs = _render_eval("--set", "harness.enabled=true")
    cr = next(d for d in docs if d["kind"] == "ClusterRole" and d["metadata"]["name"] == "agent-evaluation-reconciler")
    by_res = {tuple(r["resources"]): r["verbs"] for r in cr["rules"]}
    assert by_res[("rollouts",)] == ["get"]
    assert by_res[("analysisruns",)] == ["get", "list", "watch"]
    assert by_res[("services",)] == ["get"]


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_endpoint_service_and_network_policy_admit_only_the_rollouts_controller():
    docs = _render_eval("--set", "harness.enabled=true")
    svc = next(d for d in docs if d["kind"] == "Service" and d["metadata"]["name"] == "agent-evaluation-harness-api")
    assert svc["spec"]["type"] == "ClusterIP" and svc["spec"]["ports"][0]["port"] == 8081
    pol = next(d for d in docs if d["kind"] == "NetworkPolicy")
    rule = pol["spec"]["ingress"][0]
    assert rule["from"] == [
        {
            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "argo-rollouts"}},
            "podSelector": {"matchLabels": {"app.kubernetes.io/name": "argo-rollouts"}},
        }
    ]
    assert rule["ports"] == [{"protocol": "TCP", "port": 8081}]
    # The policy isolates the pod, so the metrics port needs its own rule (no `from`).
    assert pol["spec"]["ingress"][1] == {"ports": [{"protocol": "TCP", "port": 9090}]}
    assert not [d for d in _render_eval() if d["metadata"]["name"] == "agent-evaluation-harness-api"]


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_deployment_carries_no_bifrost_admin_or_mint_settings():
    docs = _render_eval("--set", "harness.enabled=true")
    dep = next(d for d in docs if d["kind"] == "Deployment")
    env = {e["name"]: e.get("value") for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}
    for gone in ("HARNESS_JUDGE_KEY_MINT", "BIFROST_ADMIN_URL", "HARNESS_JUDGE_BUDGET_USD",
                 "HARNESS_JUDGE_RATE_LIMIT_REQUESTS", "HARNESS_JUDGE_ALLOWED_MODELS", "HARNESS_HISTORY_LIMIT"):
        assert gone not in env
    assert env["HARNESS_API_PORT"] == "8081" and env["HARNESS_SLOT_WAIT_SECONDS"] == "300"
    ports = {p["name"]: p["containerPort"] for p in dep["spec"]["template"]["spec"]["containers"][0]["ports"]}
    assert ports["harness-api"] == 8081


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_render_fails_on_replicas_run_time_cap_and_mint():
    assert "replicaCount 1" in _fails("--set", "replicaCount=2").stderr
    assert "at most 2100" in _fails("--set", "harness.slotWaitSeconds=400").stderr
    assert "no longer supported" in _fails("--set", "harness.judge.virtualKey.mint=true").stderr
    assert _fails().returncode == 0


def test_dashboard_json_copies_are_identical():
    a = CHART / "files/agent-evaluation-dashboard.json"
    b = CHART.parents[3] / "docs/architecture/diagrams/src/agent-evaluation-dashboard.json"
    assert a.read_bytes() == b.read_bytes()


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_verdict_service_routes_to_a_not_ready_pod_and_metrics_service_does_not():
    docs = _render_eval("--set", "harness.enabled=true")
    services = {d["metadata"]["name"]: d for d in docs if d["kind"] == "Service"}
    assert services["agent-evaluation-harness-api"]["spec"]["publishNotReadyAddresses"] is True
    others = [n for n in services if n != "agent-evaluation-harness-api"]
    assert others and all("publishNotReadyAddresses" not in services[n]["spec"] for n in others)
    off = [d for d in _render_eval() if d["kind"] == "Service"]
    assert "agent-evaluation-harness-api" not in {d["metadata"]["name"] for d in off}
