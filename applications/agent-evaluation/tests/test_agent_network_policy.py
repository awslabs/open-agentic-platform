"""The agent component's NetworkPolicy admits the harness runner only when asked.

Harness Jobs call <agent>-preview directly, so an agent with an enabled harness
block needs one more ingress rule; every other agent must keep the gateway-only
policy. Rendered through the real definitions (helm template of the
oam-agent-components chart, then `vela dry-run --offline`).
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
EXAMPLE_APP = ROOT / "platform/oam/examples/example-agent-harness.yaml"

GATEWAY_RULE = {
    "from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "agentgateway-system"}}}],
    "ports": [{"port": 8083, "protocol": "TCP"}],
}
HARNESS_RULE = {
    "from": [
        {
            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "agent-evaluation"}},
            "podSelector": {"matchLabels": {"harness.oap.io/run": "true"}},
        }
    ],
    "ports": [{"port": 8083, "protocol": "TCP"}],
}

pytestmark = pytest.mark.skipif(
    shutil.which("helm") is None or shutil.which("vela") is None,
    reason="helm and vela are needed to render the agent component",
)


@pytest.fixture(scope="module")
def render(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("agent-np")
    # vela dry-run --offline still loads a kubeconfig; point it at nothing.
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
        check=True,
        capture_output=True,
    )
    env = {**os.environ, "KUBECONFIG": str(kubeconfig)}

    def _render(app: dict) -> dict:
        path = tmp / f"app-{id(app)}.yaml"
        path.write_text(yaml.safe_dump(app), encoding="utf-8")
        out = subprocess.run(
            ["vela", "dry-run", "--offline", "-d", str(defs / "oam-agent-components/templates"), "-f", str(path)],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        ).stdout
        docs = [d for d in yaml.safe_load_all(out) if isinstance(d, dict)]
        return next(d for d in docs if d.get("kind") == "NetworkPolicy")

    return _render


def _example(harness):
    app = yaml.safe_load(EXAMPLE_APP.read_text(encoding="utf-8"))
    app = copy.deepcopy(app)
    props = app["spec"]["components"][0]["properties"]
    if harness is None:
        props.pop("harness", None)
    else:
        props["harness"] = harness
    return app


def test_enabled_harness_admits_only_the_runner_pods(render):
    app = yaml.safe_load(EXAMPLE_APP.read_text(encoding="utf-8"))
    assert app["spec"]["components"][0]["properties"]["harness"]["enabled"] is True
    policy = render(app)
    assert policy["spec"]["ingress"] == [GATEWAY_RULE, HARNESS_RULE]


def test_agent_without_a_harness_block_keeps_the_gateway_only_policy(render):
    policy = render(_example(None))
    assert policy["spec"]["ingress"] == [GATEWAY_RULE]
    assert policy["spec"]["policyTypes"] == ["Ingress"]


def test_disabled_harness_keeps_the_gateway_only_policy(render):
    harness = yaml.safe_load(EXAMPLE_APP.read_text(encoding="utf-8"))["spec"]["components"][0]["properties"]["harness"]
    policy = render(_example({**harness, "enabled": False}))
    assert policy["spec"]["ingress"] == [GATEWAY_RULE]
