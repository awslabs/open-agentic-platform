"""The bifrost chart closes the management API and keeps no password in git."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
CHART = ROOT / "gitops/addons/charts/bifrost"
VALUES = ROOT / "gitops/addons/configs/bifrost/values.yaml"

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")


def _render(*extra: str) -> list[dict]:
    out = subprocess.run(
        ["helm", "template", "bifrost", str(CHART), "-n", "bifrost", "-f", str(VALUES), *extra],
        check=True, capture_output=True, text=True,
    ).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def test_admin_login_on_and_inference_open():
    docs = _render()
    cm = next(d for d in docs if d["kind"] == "ConfigMap" and "config.json" in (d.get("data") or {}))
    auth = json.loads(cm["data"]["config.json"])["auth_config"]
    assert auth["is_enabled"] is True
    assert auth["disable_auth_on_inference"] is True
    assert auth["admin_password"] == "env.BIFROST_ADMIN_PASSWORD"


def test_no_rendered_secret_carries_a_password():
    docs = _render()
    assert not [d for d in docs if d["kind"] == "Secret" and d["metadata"]["name"] == "bifrost-admin"]
    assert "bifrost-admin\"" not in "".join(yaml.dump(d) for d in docs if d["kind"] == "Secret")


def test_admin_init_is_a_presync_hook_limited_to_one_secret():
    docs = _render()
    job = next(d for d in docs if d["kind"] == "Job" and d["metadata"]["name"] == "bifrost-admin-init")
    assert job["metadata"]["annotations"]["argocd.argoproj.io/hook"] == "PreSync"
    role = next(d for d in docs if d["kind"] == "Role" and d["metadata"]["name"] == "bifrost-admin-init")
    get_rule = next(r for r in role["rules"] if "get" in r["verbs"])
    assert get_rule["resourceNames"] == ["bifrost-admin"]


def test_admin_init_absent_when_auth_off():
    docs = _render("--set", "bifrost.bifrost.authConfig.isEnabled=false")
    assert not [d for d in docs if d["metadata"]["name"] == "bifrost-admin-init"]
