"""One cluster-secret label turns agent guardrails on in three charts.
agent-gateway, crossplane-agentcore and oam-agent-components each get their
guardrails switch from a registry entry. If they could disagree, the agent
component would accept guardrails: enabled on a cluster with no guardrail
route, or the gateway would render routes no agent can reach. This test
evaluates the three registry expressions for each label combination (with
helm's template engine, the same Go text/template and sprig the
ApplicationSet controller uses) and compares them.
"""
from __future__ import annotations
import shutil
from pathlib import Path
import pytest
import yaml
from tests.test_harness_enablement import _evaluate
ROOT = Path(__file__).resolve().parents[3]
REGISTRY = ROOT / "gitops/addons/registry"
CHARTS = ROOT / "gitops/addons/charts"
ENVIRONMENTS = ROOT / "gitops/overlays/environments"
needs_helm = pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
T, F, N = "true", "false", None
# (enable_agent_guardrails, enable_agent_gateway, enable_agent_core) -> on.
# None is an absent label (the fleet-secret chart writes only true flags).
CASES = [
    (N, N, N, False),
    (T, N, N, False),
    (T, T, N, False),  # no agent_core
    (T, N, T, False),  # no gateway
    (T, T, T, True),
    (F, T, T, False),
    (N, T, T, False),  # gateway and core alone imply nothing
]
def _entry(file: str, key: str) -> dict:
    return yaml.safe_load((REGISTRY / file).read_text(encoding="utf-8"))[key]["valuesObject"]
def _expressions() -> dict:
    return {
        "agent-gateway": _entry("gateway.yaml", "agent-gateway")["guardrails"]["enabled"],
        "crossplane-agentcore": _entry("agentcore.yaml", "crossplane-agentcore")["guardrails"]["enabled"],
        "oam-agent-components": _entry("agentcore.yaml", "oam-agent-components")["global"]["guardrails"]["enabled"],
    }
def test_all_registry_entries_use_one_expression():
    exprs = _expressions()
    assert len(set(exprs.values())) == 1
    expr = next(iter(exprs.values()))
    for label in ("enable_agent_guardrails", "enable_agent_gateway", "enable_agent_core"):
        assert label in expr
@pytest.mark.parametrize("env", ["control-plane", "dev", "prod"])
def test_guardrails_flag_is_off_in_every_environment(env):
    addons = yaml.safe_load((ENVIRONMENTS / env / "enabled-addons.yaml").read_text(encoding="utf-8"))
    assert addons["enabledAddons"]["agent_guardrails"] is False
def test_chart_defaults_are_off():
    gw = yaml.safe_load((CHARTS / "agent-gateway/values.yaml").read_text())
    xp = yaml.safe_load((CHARTS / "crossplane-agentcore/values.yaml").read_text())
    oam = yaml.safe_load((CHARTS / "oam-agent-components/values.yaml").read_text())
    assert gw["guardrails"]["enabled"] == "false"
    assert xp["guardrails"]["enabled"] == "false"
    assert oam["global"]["guardrails"]["enabled"] == "false"
@needs_helm
@pytest.mark.parametrize("guardrails, gateway, core, on", CASES)
def test_all_switches_follow_the_labels_and_agree(tmp_path, guardrails, gateway, core, on):
    labels = {
        "enable_agent_guardrails": guardrails,
        "enable_agent_gateway": gateway,
        "enable_agent_core": core,
    }
    values = {
        name: _evaluate(expr, labels, tmp_path / name)
        for name, expr in _expressions().items()
    }
    assert set(values.values()) == {"true" if on else "false"}, values
