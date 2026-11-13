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
# The chart's harness.maxCases default.
MAX_CASES = 50


def test_the_chart_ships_at_least_one_dataset():
    assert DATASETS, f"no dataset under {CHART}/files/harness/datasets"


@pytest.mark.parametrize("path", DATASETS, ids=lambda p: p.stem)
def test_every_shipped_dataset_loads(path):
    dataset = load_dataset(path, max_cases=MAX_CASES)
    assert dataset.name == path.stem
    assert dataset.cases


def test_expected_answer_prompt_has_every_placeholder_and_renders():
    template = (CHECKS_DIR / "expected-answer.md").read_text(encoding="utf-8")
    for placeholder in ("{{input}}", "{{expected}}", "{{output}}", "{{nonce}}"):
        assert placeholder in template
    rendered = render_prompt(template, "IN-MARK", "EXP-MARK", "OUT-MARK", "NONCE-MARK")
    assert "{{" not in rendered
    for marker in ("IN-MARK", "EXP-MARK", "OUT-MARK"):
        assert marker in rendered
    # Every BEGIN/END marker carries the nonce, so data cannot close a fence.
    for line in rendered.splitlines():
        if line.startswith(("BEGIN ", "END ")):
            assert line.endswith(" NONCE-MARK"), line


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
