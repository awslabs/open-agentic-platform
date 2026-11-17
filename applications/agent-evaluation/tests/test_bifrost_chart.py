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


def test_minter_off_by_default():
    docs = _render()
    assert not [d for d in docs if "vk-minter" in d["metadata"]["name"]]


def test_minter_renders_hook_job_and_cronjob_with_admin_secret_in_bifrost_only():
    docs = _render("--set", "judgeKey.enabled=true")
    job = next(d for d in docs if d["kind"] == "Job" and d["metadata"]["name"] == "bifrost-vk-minter")
    assert job["metadata"]["annotations"]["argocd.argoproj.io/hook"] == "PostSync"
    cron = next(d for d in docs if d["kind"] == "CronJob")
    assert cron["metadata"]["namespace"] == "bifrost"
    env = {e["name"]: e for e in cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["BIFROST_ADMIN_PASSWORD"]["valueFrom"]["secretKeyRef"]["name"] == "bifrost-admin"
    # No Role in the bifrost chart grants the minter anything: its only grants
    # are in the evaluation namespace, rendered by the agent-evaluation chart.
    assert not [d for d in docs if d["kind"] in ("Role", "RoleBinding") and "minter" in d["metadata"]["name"]]


def test_minter_needs_admin_auth():
    r = subprocess.run(
        ["helm", "template", "b", str(CHART), "-n", "bifrost", "--set", "judgeKey.enabled=true"],
        capture_output=True, text=True,
    )
    assert r.returncode != 0 and "authConfig" in r.stderr


OTEL_CHART = ROOT / "gitops/addons/charts/otel-collector"


def test_metrics_proxy_serves_only_the_collector_namespace():
    docs = _render()
    net = next(d for d in docs if d["kind"] == "NetworkPolicy" and d["metadata"]["name"] == "bifrost-metrics")
    sel = net["spec"]["ingress"][0]["from"][0]["namespaceSelector"]["matchLabels"]
    assert sel == {"kubernetes.io/metadata.name": "otel"}
    dep = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "bifrost-metrics")
    assert dep["spec"]["template"]["spec"]["automountServiceAccountToken"] is False


def test_collector_scrapes_the_proxy_and_holds_no_credential():
    for args in ([], ["--set", "amp.secretManagerKey=x"]):
        out = subprocess.run(
            ["helm", "template", "o", str(OTEL_CHART), "-n", "otel", *args],
            check=True, capture_output=True, text=True,
        ).stdout
        assert "bifrost-metrics.bifrost.svc.cluster.local:8080" in out
        assert "basic_auth" not in out and "password" not in out.lower()


def test_proxy_script_builds_basic_header(monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("metrics_proxy", CHART / "files/metrics-proxy.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setenv("BIFROST_ADMIN_USERNAME", "u")
    monkeypatch.setenv("BIFROST_ADMIN_PASSWORD", "p")
    assert mod.auth_header() == "Basic dTpw"
