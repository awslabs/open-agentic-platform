"""Agent identity at the gateway: who may reach which agent or MCP server.

Needs examples/mcp-servers/my-assistant.yaml (agent oap-assistant-a) and the mcp-time
server with allowedAgents [oap-assistant-a], plus the dev overlay's mcpAccess grant.
"""

import json
import time

import pytest

from conftest import REPO, claims, subject

pytestmark = pytest.mark.identity

AGENT, AGENT_NS, MCP, OTHER_SA = "oap-assistant-a", "default", "mcp-time", "default"
SA_PREFIX = "system:serviceaccount:"
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "live-tests", "version": "1"}}}
SAY_HI = {"message": "Say hi."}


@pytest.fixture(scope="module")
def tokens(user_token, sa_token):
    return {
        "user": user_token("user1"),
        "agent": sa_token(AGENT_NS, AGENT),
        "other": sa_token(AGENT_NS, OTHER_SA),
        "wrong-aud": sa_token(AGENT_NS, AGENT, audience="not-the-gateway"),
        "none": None,
    }


@pytest.fixture(scope="module")
def gateway_rule(custom):
    p = custom.get_namespaced_custom_object("agentgateway.dev", "v1alpha1", "agentgateway-system",
                                            "agentgatewaypolicies", "jwt-auth-policy")
    return p["spec"]["traffic"]["authorization"]["policy"]["matchExpressions"][0]


def test_I1_workload_branch_pins_issuer(gateway_rule, tokens):
    iss = claims(tokens["agent"])["iss"]
    assert f'jwt.iss == "{iss}"' in gateway_rule


def test_I2_human_branch_excludes_serviceaccount_subjects(gateway_rule):
    assert f'&& !(has(jwt.sub) && jwt.sub.startsWith("{SA_PREFIX}"))' in gateway_rule


def test_G1_mcp_access_grant_is_a_require_listing_the_agent(custom):
    p = custom.get_namespaced_custom_object("agentgateway.dev", "v1alpha1", AGENT_NS,
                                            "agentgatewaypolicies", f"{MCP}-access")
    a = p["spec"]["traffic"]["authorization"]
    assert a["action"] == "Require"
    assert f"{SA_PREFIX}{AGENT_NS}:{AGENT}" in a["policy"]["matchExpressions"][0]


ACCESS = [
    # id, caller, method, path, body, expected status
    ("U1", "user", "POST", f"/{AGENT}/chat", SAY_HI, 200),
    ("U2", "none", "POST", f"/{AGENT}/chat", SAY_HI, 401),
    ("U3", "user", "POST", f"/mcp/{MCP}", INIT, 403),  # MCP server with allowedAgents: not an agent
    ("W1", "agent", "POST", f"/mcp/{MCP}", INIT, 200),  # listed agent
    ("W2", "other", "POST", f"/mcp/{MCP}", INIT, 403),  # unlisted ServiceAccount
    ("W3", "other", "POST", f"/{AGENT}/chat", SAY_HI, 200),  # by design: agents serve any authenticated caller
    ("W4", "wrong-aud", "POST", f"/{AGENT}/chat", SAY_HI, 401),
]


@pytest.mark.parametrize("row,caller,method,path,body,expected", ACCESS, ids=[r[0] for r in ACCESS])
def test_access(gw, tokens, row, caller, method, path, body, expected):
    assert gw.request(tokens[caller], method, path, body, timeout=120).status_code == expected


def test_E1_agent_calls_mcp_tool_with_its_own_identity(gw, tokens, gateway_logs):
    # The model sometimes answers in text instead of calling the tool, so ask up to twice
    # and judge by the gateway log: the agent reached the MCP server as itself.
    sub = f"{SA_PREFIX}{AGENT_NS}:{AGENT}"

    def hits():
        return sum(1 for line in gateway_logs().splitlines()
                   if f"route={AGENT_NS}/{MCP} " in line and f"jwt.sub={sub} " in line
                   and "http.method=GET" not in line and "http.status=200" in line)

    before = hits()
    for _ in range(2):
        gw.chat(tokens["user"], AGENT, None, "What time is it in UTC right now? Call your time tool. Reply with the time only.")
        if hits() > before:
            break
    assert hits() > before


def test_E2_every_agent_pod_logs_its_mcp_identity_mode(core):
    pods = core.list_namespaced_pod(AGENT_NS, label_selector=f"app.kubernetes.io/name={AGENT}").items
    assert pods
    for p in pods:
        log = core.read_namespaced_pod_log(p.metadata.name, AGENT_NS, container=AGENT)
        assert "MCP identity: agent ServiceAccount token" in log, p.metadata.name


def test_D1_browser_mcp_example_passes_admission(kubectl):
    # Issue #61: an MCP server Application that omits authPolicy and uses the
    # aws-service-identity trait was rejected at admission. Server-side dry run goes
    # through the KubeVela webhook, the path ArgoCD takes.
    r = kubectl("apply", "--dry-run=server", "-f", str(REPO / "platform/oam/examples/example-browser-mcp.yaml"))
    assert r.returncode == 0, r.stderr


@pytest.mark.skip(reason="K1 needs Keycloak to mint a ServiceAccount-shaped sub (a protocol mapper); I1/I2 check the rule instead")
def test_K1_keycloak_token_with_serviceaccount_sub_is_denied():
    pass
