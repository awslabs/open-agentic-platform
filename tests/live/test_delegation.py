"""Supervisor agents delegating to other agents (docs/agent-delegation/DESIGN.md).

Applies tests/live/fixtures/delegation.yaml (namespace default) and leaves it running;
set KEEP=0 to remove it afterwards. Also needs the AgentCore memory example
(platform/oam/examples/example-agent-agentcore-memory.yaml, agent my-agent) and the dev
overlay's delegation and agentAccess entries for the fixture.

The supervisor deleg-supervisor lists:
  deleg-math        granted to the supervisor only (agentAccess)
  my-agent          no agentAccess entry; has per-user AgentCore memory
  deleg-restricted  granted to another agent only, so the supervisor is denied
  deleg-ghost       not deployed
"""

import os
import re
import time
from pathlib import Path

import pytest

from conftest import REPO, restarts, subject

pytestmark = pytest.mark.delegation

NS, SUP, MATH, MEM, MEM_NS = "default", "deleg-supervisor", "deleg-math", "my-agent", "agents"
SUP_SUB = f"system:serviceaccount:{NS}:{SUP}"
OTHER_SUB = f"system:serviceaccount:{NS}:default"
FIXTURE = Path(__file__).parent / "fixtures" / "delegation.yaml"
RUN = str(int(time.time()))


@pytest.fixture(scope="module")
def deployed(kubectl):
    r = kubectl("apply", "-f", str(FIXTURE))
    assert r.returncode == 0, r.stderr
    for name in (MATH, "deleg-restricted", SUP):
        # A pod may not exist yet right after apply; wait for it, then for Ready.
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            w = kubectl("wait", "--for=condition=Ready", "pod", "-l", f"app.kubernetes.io/name={name}",
                        "-n", NS, "--timeout=60s", timeout=90)
            if w.returncode == 0:
                break
            time.sleep(5)
        else:
            pytest.fail(f"{name} not Ready within 240s")
    yield
    if os.getenv("KEEP", "1") == "0":
        kubectl("delete", "-f", str(FIXTURE), "--wait=false")


@pytest.fixture(scope="module")
def t(deployed, user_token, sa_token):
    u1, u2 = user_token("user1"), user_token("user2")
    return {"u1": u1, "u2": u2, "s1": subject(u1), "s2": subject(u2),
            "sup": sa_token(NS, SUP), "other": sa_token(NS, "default")}


@pytest.fixture(scope="module")
def runs(gw, t):
    """Every delegated conversation the rows inspect, run once, in order."""
    out = {}
    out["d1"] = gw.chat(t["u1"], SUP, f"d1{RUN}", "What is 1234 * 5678? Use your agents.")
    out["d2"] = gw.chat(t["u1"], SUP, f"d2{RUN}",
                        f"Two things: ask the assistant agent ({MEM}) to remember that my favourite fruit is "
                        "mango, and compute 17 * 23.")
    out["m2"] = gw.chat(t["u2"], SUP, f"m2{RUN}", f"Ask the assistant agent ({MEM}) what my favourite fruit is.")
    out["i2"] = gw.chat(t["other"], SUP, f"i2{RUN}", f"Ask the assistant agent ({MEM}) to say hello.")
    out["i3"] = gw.chat(t["u1"], MEM, f"i3{RUN}", "Say hi.",
                        headers={"X-Delegated-User": t["s2"], "X-Forwarded-Agent": SUP_SUB})
    out["i4"] = gw.chat(t["other"], MEM, f"i4{RUN}", "Say hi.", headers={"X-Delegated-User": t["s1"]})
    for line in (f"{k}: {v[0]} {v[1][:160]}" for k, v in out.items()):
        print(line)
    return out


@pytest.fixture(scope="module")
def mem_log(runs, logs):
    return logs(MEM_NS, MEM)


@pytest.fixture(scope="module")
def sup_log(runs, logs):
    return logs(NS, SUP)


def actor(mem_log, raw_context):
    """The memory actor my-agent's session manager used for *raw_context*."""
    found = re.findall(rf"actor=([^,]*),[^\n]*raw_context='{re.escape(raw_context)}'", mem_log)
    return found[-1] if found else None


def gateway_hits(gateway_logs, route):
    return sum(1 for line in gateway_logs().splitlines()
               if f"route={route} " in line and f"jwt.sub={SUP_SUB} " in line and "http.status=200" in line)


# ── policy at the gateway: card fetches ──────────────────────────────────

CARDS = [
    # id, caller, agent, expected statuses
    ("P1", ["sup"], MATH, [200]),                       # granted
    ("P2", ["other", "u1"], MATH, [403, 403]),          # supervisor-only grant
    ("P3", ["other", "u1"], MEM, [200, 200]),           # no agentAccess entry
    ("P4", ["sup"], "deleg-restricted", [403]),         # granted to another agent
]


@pytest.mark.parametrize("row,callers,agent,expected", CARDS, ids=[c[0] for c in CARDS])
def test_card_access(gw, t, row, callers, agent, expected):
    got = [gw.request(t[c], "GET", f"/{agent}/.well-known/agent-card.json").status_code for c in callers]
    assert got == expected


# ── delegation ───────────────────────────────────────────────────────────

def test_D1_arithmetic_goes_to_the_math_agent(runs, gateway_logs):
    status, reply = runs["d1"]
    assert status == 200
    assert gateway_hits(gateway_logs, f"{NS}/{MATH}") >= 1
    assert "7006652" in reply.replace(",", "").replace(" ", "")


def test_C1_reachable_agents_only(runs, sup_log):
    lines = [l for l in sup_log.splitlines() if f"Sub-agents for session d1{RUN}:" in l]
    assert lines, "no sub-agent line for the d1 session"
    assert lines[-1].endswith(f"available={MATH},{MEM} unavailable=deleg-restricted,deleg-ghost")


def test_D2_two_part_task_reaches_both(runs, gateway_logs):
    status, reply = runs["d2"]
    assert status == 200
    assert gateway_hits(gateway_logs, f"{MEM_NS}/{MEM}") >= 1
    assert "391" in reply


# ── identity at the sub-agent ────────────────────────────────────────────

def test_I1_delegated_call_names_user_and_supervisor(mem_log, t):
    assert re.search(rf"session=d2{RUN}-{MEM} delegated_by={re.escape(SUP_SUB)} user={re.escape(t['s1'])}", mem_log)


def test_M1_sub_agent_memory_actor_is_the_end_user(mem_log, t):
    assert actor(mem_log, f"d2{RUN}-{MEM}") == t["s1"]


def test_M2_second_user_gets_their_own_actor(mem_log, t):
    assert actor(mem_log, f"m2{RUN}-{MEM}") == t["s2"]


def test_I2_workload_caller_is_the_user_the_sub_agent_sees(mem_log):
    assert actor(mem_log, f"i2{RUN}-{MEM}") == OTHER_SUB


def test_I3_client_sent_delegation_headers_are_ignored(mem_log, t):
    assert actor(mem_log, f"i3{RUN}") == t["s1"]
    assert not re.search(rf"session=i3{RUN} delegated_by=", mem_log)


def test_I4_agent_not_allowed_to_delegate_cannot_name_a_user(mem_log):
    assert actor(mem_log, f"i4{RUN}") == OTHER_SUB


# ── guardrails ───────────────────────────────────────────────────────────

def test_G1_depth_limit_stops_delegation(gw, t, logs, runs):
    # The model may answer without calling a tool, so ask up to twice.
    for i in range(2):
        gw.chat(t["u1"], SUP, f"g1{RUN}{i}", "What is 99 * 101? Use your agents.",
                headers={"X-Delegation-Depth": "3"})
        if "refused: depth limit" in logs(NS, SUP):
            break
    assert "refused: depth limit" in logs(NS, SUP)


@pytest.mark.skip(reason="G2 (sub-agent timeout reported as a gap) is covered by tests/unit/test_delegation.py only")
def test_G2_timeout_is_reported_as_a_gap():
    pass


# ── shipped example and health ───────────────────────────────────────────

def test_X1_example_passes_admission(kubectl):
    r = kubectl("apply", "--dry-run=server", "-f", str(REPO / "platform/oam/examples/example-supervisor-agent.yaml"))
    assert r.returncode == 0, r.stderr


def test_H1_no_restarts(core, runs):
    assert restarts(core, NS, SUP) == 0 and restarts(core, MEM_NS, MEM) == 0
