"""helm template helpers shared by the test_guardrails_*.py chart tests."""
from __future__ import annotations
import shutil
import subprocess
from pathlib import Path
import pytest
import yaml
ROOT = Path(__file__).resolve().parents[3]
CHARTS = ROOT / "gitops/addons/charts"
needs_helm = pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def run(chart: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["helm", "template", "t", str(CHARTS / chart), *args],
        capture_output=True, text=True,
    )
def render_text(chart: str, *args: str) -> str:
    out = run(chart, *args)
    assert out.returncode == 0, out.stderr
    return out.stdout
def render(chart: str, *args: str) -> list[dict]:
    return [d for d in yaml.safe_load_all(render_text(chart, *args)) if d]
def of_kind(docs: list[dict], kind: str) -> list[dict]:
    return [d for d in docs if d["kind"] == kind]
