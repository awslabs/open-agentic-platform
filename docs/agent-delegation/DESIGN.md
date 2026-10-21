# Supervisor agents: delegating to other agents

Status: draft for review. Nothing here is built yet.

## Goal

A developer lists other agents on an agent, the same way they list MCP servers:

```yaml
components:
  - name: incident-supervisor
    type: agent
    properties:
      description: Triages incidents end to end
      systemMessage: |
        Goal: triage the incident. Gather the relevant logs, find the cause in the
        cluster state, and propose a fix with the evidence for it.
      agents:
        - name: log-analyst
        - name: k8s-diagnostics
```

The agent then works as a supervisor. It learns what each listed agent can do,
delegates matching parts of the task to them, combines their results, and checks the
result against the goal in its prompt. The sub-agents are ordinary agents; nothing
changes in them.

## Decisions

| | Decision |
|---|---|
| Declaring sub-agents | An explicit `agents` list on the `agent` component, like `mcpServers`. Any agent registered on the gateway can be listed. No automatic discovery of every agent. |
| Learning capabilities | The supervisor fetches each listed agent's A2A agent card through the gateway and turns it into one tool. |
| Identity at the sub-agent | The supervisor calls with its own ServiceAccount token, on behalf of the end user. The sub-agent sees the end user in `X-Forwarded-User` and the supervisor in `X-Forwarded-Agent`. |
| Trust in the delegated user | Asserted by the supervisor, accepted by the gateway only from agents allowed to delegate. Verifying the user's token at the gateway is a follow-up (see "Trust model"). |
| Who may call an agent | `agentAccess` in the agent-gateway values, mirroring `mcpAccess`. An agent with no entry accepts any authenticated caller, as today. |
| Delegated calls | Transitive, like MCP: a delegated call is authorized on the supervisor, not re-checked on the user. |
| Scope | Same cluster, through that cluster's gateway. Cross-cluster delegation is out of scope. |
| Orchestration | Model-driven (agents as tools), not a fixed graph. The workflow goal is the agent's `systemMessage`. |

## What exists today

- An agent lists MCP servers in `mcpServers` (`platform/oam/definitions/components/agent.cue:498`).
  The component joins the names into `MCP_SERVER_NAMES` (`agent.cue:187`), and the image
  builds `<GATEWAY_URL>/mcp/<name>` for each (`applications/strands-agent-base/app/config.py:100`).
- Every agent with `registerWithGateway` (default true, `agent.cue:450`) gets an HTTPRoute
  on the gateway at the path prefix `/<name>` (`agent.cue:375`), rewritten to `/`.
- Every agent serves an A2A agent card at `/.well-known/agent-card.json` (a2a-sdk
  `AGENT_CARD_WELL_KNOWN_PATH`), so through the gateway at
  `<GATEWAY_URL>/<name>/.well-known/agent-card.json`.
- The agent's `systemMessage` becomes `SYSTEM_PROMPT` (`agent.cue:151`).
- MCP calls carry the agent's own ServiceAccount token by default, and `mcpAccess`
  (`gitops/addons/charts/agent-gateway/templates/mcp-access.yaml`) is a `Require` rule on
  the server's route listing the agents allowed to call it. A user who can reach an
  agent gets that agent's tools; the user is not re-checked at the MCP server.
- Any authenticated caller, user or ServiceAccount, may call any agent route
  (`scripts/test-identity-matrix.sh` row W3).
- Strands 1.57 ships an A2A client, `A2AAgent` (`strands/agent/a2a_agent.py`). Unlike a
  local `Agent`, it has no `as_tool()`, so the supervisor wraps it in its own tool.

Depends on PR #66, which adds:
- the gateway policy that sets `X-Forwarded-User` to the caller's `sub` on every request
  (`gitops/addons/charts/agent-gateway/templates/caller-identity-policy.yaml`);
- per-caller memory and A2A isolation keyed on that header (`app/identity.py`,
  `app/a2a_isolation.py`).

## Design

### 1. OAM: the `agents` parameter

```cue
// Agents this agent may delegate to, by component name. Each must be registered on
// the gateway (registerWithGateway). Bare name: an agent route on this cluster's gateway.
agents: *[] | [...{name: string}]
```

The component emits `A2A_AGENT_NAMES=<name>,<name>`, built like `MCP_SERVER_NAMES`. The
agent card ConfigMap (`agent.dev/type: agent-card`) records the list, as it does for
`mcpServers`. Nothing else in the component changes: a supervisor is an agent with a
non-empty `agents` list.

No URLs, regions or cluster names appear in the Application, so it stays portable
(`oam-authoring.md` section 1).

### 2. Capabilities: agent cards become tools

At startup, and again on a timer (5 minutes, like the memory strategy refresh in #66),
the supervisor fetches `<GATEWAY_URL>/<name>/.well-known/agent-card.json` for each
listed name, presenting its own ServiceAccount token. Each card becomes one tool:

- name: `ask_<name>` (normalised to the tool-name pattern);
- description: the card's description plus its skills (names, descriptions, examples);
- input: the subtask as text;
- behaviour: send an A2A `message/send` to `<GATEWAY_URL>/<name>/` and return the reply
  text, or an error the model can act on.

A card that cannot be fetched (agent missing, not ready, or denied by `agentAccess`) is
logged with the reason and the tool is left out. The supervisor still starts, the same
way caller MCP pools degrade today. A later refresh adds the tool when the agent appears.
Readiness gating on sub-agents, like `/ready` does for the workload MCP pool, is not in
the first version; it is a choice to revisit if autonomous supervisors need a complete
roster to start.

### 3. Supervisor behaviour

The platform appends a short instruction to `SYSTEM_PROMPT` when `A2A_AGENT_NAMES` is set:
plan the steps the goal needs, delegate a step to the agent whose card fits it, call
independent agents in the same turn so they run concurrently, combine the results, and
check the combined answer against the goal. It says which agents were unavailable, so
the model reports a gap instead of guessing.

Context: each (supervisor session, sub-agent) pair uses a stable A2A `contextId`, so a
follow-up to the same sub-agent continues its thread. Sub-agent contexts are pod-local
on the sub-agent, like every A2A context today, so sub-agents run with one replica.

### 4. Identity: on behalf of the user

Today the gateway overwrites `X-Forwarded-User` with `jwt.sub` on every hop. Once the
supervisor calls with its own token, the sub-agent would see the supervisor as its
caller, and its memory would be one shared actor for all users.

The supervisor therefore sends, on each delegated call:

| Header | Value |
|---|---|
| `Authorization` | its own ServiceAccount token (as for MCP) |
| `X-Delegated-User` | the `X-Forwarded-User` value it received for this request |
| `X-Delegation-Depth` | depth of this call (see Guardrails) |

and the gateway's identity policy becomes:

| Caller (`jwt.sub`) | `X-Forwarded-User` set to | `X-Forwarded-Agent` set to |
|---|---|---|
| a delegating agent, header present | `X-Delegated-User` | `jwt.sub` |
| a delegating agent, no header (autonomous run) | `jwt.sub` | `jwt.sub` |
| any other agent | `jwt.sub` (header ignored) | `jwt.sub` |
| a user | `jwt.sub` (header ignored) | removed |

`X-Delegated-User` is removed before the request reaches any backend. "Delegating agent"
is a list in the agent-gateway values (`delegation.agents`), granted per environment,
like `mcpAccess`.

The sub-agent needs no change: memory and A2A isolation key on `X-Forwarded-User`, so
they stay per end user. `X-Forwarded-Agent` is available for traces and audit.

An agent called by a supervisor and itself delegating passes `X-Delegated-User` on, so
the user survives more than one hop.

Unverified, and the first thing to check (step 1): whether an agentgateway request
transformation can read a request header and pick a value conditionally on `jwt.sub`.
`caller-identity-policy.yaml` today sets a header from a CEL expression (`jwt.sub`). If
conditional expressions or header reads are not supported, this section needs another
mechanism before anything else is built.

### 5. Trust model

The gateway trusts the supervisor's claim about the user. A compromised supervisor that
is allowed to delegate could therefore claim any user toward the agents it may call, and
read that user's memory there. This is the same trust the platform already places in an
agent with MCP access: the agent acts with its own grants, whoever its caller is.

Mitigations in the first version: only agents listed in `delegation.agents` are
believed; `agentAccess` limits which agents a supervisor can reach at all; the gateway
removes the header from everyone else.

The stronger model, verifying the user's own token at the gateway (token exchange, ADR-6
in `docs/architecture/agent-identity-and-token-exchange.md`), is a follow-up issue.

### 6. Policy: `agentAccess`

Same shape and semantics as `mcpAccess`:

```yaml
agentAccess:
  k8s-diagnostics:
    namespace: agents
    agents: [incident-supervisor, ops/other-supervisor]   # bare name = same namespace
```

It renders one `AgentgatewayPolicy` per agent: `traffic.authorization`, `action: Require`,
on the agent's HTTPRoute, with one expression listing the allowed ServiceAccount
subjects. `Require` ANDs with the Gateway-wide rule in `jwt-policy.yaml`, as
`mcp-access.yaml` explains; `Allow` would be ORed with it and restrict nothing.

| Caller | Agent has an `agentAccess` entry | Result |
|---|---|---|
| a listed agent | yes | allowed |
| an unlisted agent | yes | 403 |
| a user, direct call | yes | 403, unless users are also granted (see below) |
| anyone authenticated | no | allowed (today's behaviour) |

Delegated calls are transitive, like MCP: the gateway authorizes the supervisor and does
not re-check the delegated user at the sub-agent. A user who can use the supervisor can
use what it delegates to.

Open point for implementation: whether an `agentAccess` entry also needs a way to keep
direct user calls, for an agent that is both a sub-agent and called by people. The likely
shape is an optional `users: any` flag; it is decided with the matrix rows.

### 7. Guardrails

- Depth: the supervisor sends `X-Delegation-Depth`, one more than the depth it received.
  An agent refuses to delegate past a maximum (default 3), so A to B to A loops end.
- Time: a per-call timeout below the gateway's request timeout, and a cap on delegations
  per turn.
- Failures: a sub-agent error or timeout returns a tool error the model can act on. It
  does not fail the supervisor's turn.

### 8. Observability

The A2A client propagates the trace context, so Langfuse shows one trace: the user's
request, the supervisor's turn, each delegation, and each sub-agent's own calls. The
supervisor tags its traces with the agents it delegated to.

## Scenario matrix

Committed as `scripts/test-delegation-matrix.sh`, run on a spoke with one supervisor and
two sub-agents. Expected outcomes:

| Row | Scenario | Expected |
|---|---|---|
| C1 | supervisor fetches cards for listed agents | one tool per listed agent, none for unlisted |
| C2 | a listed agent is not deployed | supervisor starts; tool missing; reason logged |
| D1 | task matching one sub-agent | that sub-agent is called; answer uses its result |
| D2 | task needing both, independent | both called in one turn |
| I1 | identity at the sub-agent, delegated call | `X-Forwarded-User` = end user, `X-Forwarded-Agent` = supervisor |
| I2 | autonomous run (no end user) | `X-Forwarded-User` = supervisor |
| I3 | user sends `X-Delegated-User` | stripped; `X-Forwarded-User` = the user |
| I4 | agent not in `delegation.agents` sends it | ignored; `X-Forwarded-User` = that agent |
| M1 | sub-agent memory, two users through one supervisor | separate actors; no cross-recall |
| P1 | supervisor in the sub-agent's `agentAccess` | 200 |
| P2 | supervisor not in it | 403; tool left out at card fetch |
| P3 | sub-agent without an entry | any authenticated caller 200 |
| G1 | delegation loop | stopped at the depth limit, reported |
| G2 | sub-agent times out | reported as a gap; supervisor answers |
| R1 | existing identity and memory matrices | unchanged pass |

## Plan

1. Spike: confirm the gateway transformation in section 4 against the agentgateway docs
   and live on oap-dev. Changes the design if it fails.
2. Commit the scenario matrix script.
3. Gateway: delegation rule in `caller-identity-policy.yaml`, `agentAccess` template,
   values defaults.
4. Agent image: card-to-tool wrapper, outbound headers, guardrails, unit tests, new image.
5. OAM: `agents` parameter in `agent.cue`, regenerated, and an example Application with a
   supervisor and two sub-agents.
6. Full matrix live on oap-dev, then an independent review.

Branches from `main` after #66 merges.

## Follow-ups

- Verified delegation: the gateway checks the user's own token instead of the
  supervisor's claim.
- Cross-cluster delegation.
- Readiness gating on sub-agents for autonomous supervisors.
- Restricting agent and MCP ingress to the gateway (the NetworkPolicy issue from #66),
  which also protects the delegation headers from direct callers.
