# OAM Component Design Decisions

This document captures architectural and design decisions for the KubeVela OAM components in this platform: **agent** (A2A) and **mcp-server** (MCP).

---

## Shared Decisions (Both Components)

### Blue-Green Deployment via Argo Rollouts

Both components use Argo Rollouts with blue-green strategy as the primary workload resource (not Deployment).

**How it works**:
- The Rollout creates two Services: `<name>-stable` (active) and `<name>-preview`.
- On image update, the new version deploys to preview. After promotion, it becomes stable.
- Argo Rollouts manages which ReplicaSet backs which service via selector injection.
- Default: auto-promotion enabled, 10s promotion delay, 30s scale-down delay.

**Gateway API plugin**: kgateway/agentgateway supports the Argo Rollouts Gateway API traffic router plugin (`argoproj-labs/gatewayAPI`) for canary with traffic splitting. For blue-green, the standard service selector approach works without the plugin since backends always point to the stable service.

**Reference**: [kgateway Argo Rollouts integration](https://kgateway.dev/docs/envoy/main/integrations/argo)

### KubeVela OAM Pattern

Both components follow the same structure:
- `.cue` file defines the component template with parameters
- A `ComponentDefinition` YAML wraps the CUE template for registration with KubeVela
- An `Application` YAML instantiates the component with specific properties

### Gateway Registration

Both components optionally register with agentgateway via HTTPRoute (`registerWithGateway: true` by default). The route attaches to the `agentgateway-proxy` Gateway in `agentgateway-system`.

### Service Port Convention

Services expose port 80 externally, mapping to the container's application port. This matches agentgateway documentation conventions and simplifies backend configuration.

---

## Agent Component (`agent.cue`)

The `agent` component deploys A2A (Agent-to-Agent) protocol agents.

### Generated Resources

1. **Argo Rollout** — blue-green deployment
2. **Stable Service** — `appProtocol: kgateway.dev/a2a`, port 8083
3. **Preview Service** — blue-green preview
4. **Agent Card ConfigMap** — metadata for agent discovery
5. **HTTPRoute** — routes `/<name>` to stable service (optional)

### LLM Access via Gateway (Not Direct Bedrock)

**Decision**: Agents access LLMs through a centralized LiteLLM proxy gateway, not directly via AWS Bedrock credentials.

**Why**:
- No AWS credentials needed in agent pods
- Centralized access control, monitoring, and cost tracking
- Support for multiple LLM providers behind a single endpoint
- Simplified agent deployment

Default config points to `http://litellm-proxy.agentgateway-system.svc.cluster.local:4000`.

### Container Port: 8083

Agents listen on port 8083 (A2A protocol convention). The stable service maps port 8083 → 8083 with `appProtocol: kgateway.dev/a2a`.

### MCP Server References

Agents reference MCP servers by name. The `MCP_SERVERS` env var is constructed from the `mcpServers` parameter, building service URLs as `http://<name>.<namespace>.svc.cluster.local:<port>`.

---

## MCP Server Component (`mcp-server.cue`)

The `mcp-server` component deploys Model Context Protocol servers with agentgateway integration.

### Generated Resources

1. **Argo Rollout** — blue-green deployment
2. **Stable Service** — `appProtocol: agentgateway.dev/mcp`, port 80 → containerPort
3. **Preview Service** — blue-green preview
4. **AgentgatewayBackend** — static target to stable service FQDN
5. **HTTPRoute** — registers at `/mcp/<name>` (optional)
6. **AgentgatewayPolicy** — tool-level CEL authorization (optional)

### Per-Server Routing (Not Federation)

**Decision**: Each MCP server gets its own HTTPRoute at `/mcp/<name>`, its own AgentgatewayBackend, and its own namespace.

**Rejected alternative**: Agentgateway supports federation — a single AgentgatewayBackend with multiple targets at one `/mcp` endpoint. Tools from all servers are returned on `tools/list`, prefixed with the target name (e.g., `weather_get_forecast`).

**Why we rejected federation**:
- At scale (100+ MCP servers, dozens of tools each), `tools/list` returns 1000+ tools — bloating agent context windows, increasing cost, and causing confusion.
- Teams deploy MCP servers independently in separate namespaces. A centralized backend becomes a coordination bottleneck.
- Per-server routing gives agents scoped tool lists — they connect only to the servers they need.
- Tool names remain clean (no target prefix).

**Tradeoff**: Agents need multiple MCP connections instead of one. This is acceptable — it provides isolation and scoping.

**Reference**: [agentgateway MCP federation docs](https://agentgateway.dev/docs/kubernetes/latest/tutorials/mcp-federation), [Virtual MCP docs](https://agentgateway.dev/docs/kubernetes/main/mcp/virtual/)

### Static Backend (Not Dynamic)

**Decision**: Use `static` targets in AgentgatewayBackend pointing to the stable service FQDN.

**Rejected alternative**: Dynamic backends use label selectors to discover services at runtime.

**Why**:
- Dynamic backends still require a Service — they don't bypass the Service layer.
- With Argo Rollouts blue-green, we need to explicitly target the stable service. Dynamic label selectors could match both stable and preview services.
- Static gives deterministic routing through the blue-green lifecycle.

**Reference**: [agentgateway Static MCP](https://agentgateway.dev/docs/kubernetes/latest/mcp/static-mcp/), [Dynamic MCP](https://agentgateway.dev/docs/kubernetes/latest/mcp/dynamic-mcp/)

### Default Container Port: 8000

FastMCP (the standard Python MCP server library) defaults to port 8000 for StreamableHTTP. Overridable via `containerPort` parameter.

### StreamableHTTP Protocol (Not SSE)

**Decision**: Default MCP protocol is `StreamableHTTP`, configurable to `SSE`.

**Why**: StreamableHTTP is the modern MCP transport. SSE is legacy. Agentgateway's dynamic backend only supports StreamableHTTP. Defaulting to StreamableHTTP ensures forward compatibility.

### Component Scope: Local Only

**Decision**: `mcp-server` handles locally deployed MCP servers (Deployment + Service + Backend + Route).

**Remote MCP servers** (external endpoints via HTTPS) only need an AgentgatewayBackend with a static host pointing to the external FQDN, plus an HTTPRoute. No Deployment or Service. This should be a separate component type (e.g., `mcp-remote`) if needed.

**Reference**: [agentgateway Connect via HTTPS](https://agentgateway.dev/docs/kubernetes/latest/mcp/https/)

### Authentication & Authorization

Tool-level authorization is opt-in via `authPolicy` parameter, generating an `AgentgatewayPolicy` targeting the AgentgatewayBackend.

**Two levels of auth in agentgateway**:

| Level | Policy Target | Purpose |
|---|---|---|
| **JWT auth** | Gateway or HTTPRoute | Validates JWT tokens, optional claim-based RBAC for server access |
| **Tool access** | AgentgatewayBackend | CEL expressions filtering which tools are visible per JWT claims |

**JWT auth** is configured once at the Gateway level by the platform team (not per MCP server).

**Tool access** is per-backend, configured by the MCP server owner:

```yaml
authPolicy:
  action: Allow
  matchExpressions:
    - 'jwt.sub == "alice" && mcp.tool.name == "get_time"'
    - 'jwt.team == "ops"'
```

CEL expressions use OR logic — any matching expression grants access.

**MCP Auth vs JWT Auth**: Use JWT auth for service-to-service / static clients. Use MCP auth for interactive MCP clients (MCP Inspector, VS Code, Claude Code) that need dynamic OAuth discovery.

**Reference**: [agentgateway JWT auth](https://agentgateway.dev/docs/kubernetes/latest/mcp/mcp-access/), [Tool access](https://agentgateway.dev/docs/kubernetes/latest/mcp/tool-access/)

### Route Path Convention

Each MCP server is exposed at `/mcp/<name>`. Agents connect via `http://<gateway-address>/mcp/<name>`.

## Autoscaling mcp-server components

`replicas` on `mcp-server` is optional by design. Set it and KubeVela owns the count;
omit it and an `hpa` or `cpuscaler` trait can own it. Rendering it unconditionally
made the two controllers fight, because an HPA writes `spec.replicas` through the
Rollout's `/scale` subresource while KubeVela reconciles it back to the declared value.
Measured before the fix: `spec.replicas` went 2, then 1 (killing a pod), then 2. After:
held at 2 for five minutes with zero reverts.

KubeVela's `hpa` trait does drive an Argo Rollout, even though it declares
`appliesToWorkloads: ['deployments.apps','statefulsets.apps']`, which is not enforced
on this path. Point it at the Rollout explicitly:

```yaml
traits:
  - type: hpa
    properties:
      min: 1
      max: 5
      targetAPIVersion: argoproj.io/v1alpha1
      targetKind: Rollout
      cpu:
        value: 70          # note: `value`, not `usage`
```

Two prerequisites, both easy to get wrong:

- **CPU requests must be set.** HPA utilization is a percentage of requests, so a
  container with no requests cannot be autoscaled on CPU.
- **The workload must tolerate more than one replica.** Any server holding session
  state in memory needs gateway session affinity first. `browser-mcp` demonstrates the
  failure: with 2 replicas, connect succeeds and the next call returns
  `Bad Request: no valid session ID provided`, because the follow-up POST lands on a
  pod that never saw the session.

`cpu.usage` is not a parameter. Passing it is silently ignored and the target renders
as the default 50%.

## Session affinity

MCP sessions are stateful by specification: the server returns an `Mcp-Session-Id` on
initialize and the client presents it on every later request. If the server keeps
per-session state in memory, follow-up requests must reach the same pod.

### mcp-server: on by default, and it works

`mcp-server` registers a **selector-based** `AgentgatewayBackend` target and sets
`sessionRouting` from a `sessionAffinity` parameter that defaults to `true`. The target
type is the crux. Upstream is explicit that stateful session routing and session
affinity require non-static, selector-based targets, and that a `static` target with
`sessionRouting: Stateful` carries no routing guarantee.

The component previously used a static host pointing at the stable Service, which meant
the gateway spoke to a ClusterIP and kube-proxy chose a pod per connection. Measured at
2 replicas: connect succeeded, then the next call failed with
`Bad Request: no valid session ID provided`. After the change, at 2 replicas, the same
test passed and per-pod `/readyz` showed the session pinned to exactly one pod
(`mcpSessions=1, liveBrowserSessions=1` on one, `0/0` on the other).

The selector matches an `agentgateway.dev/target` label placed only on the **stable**
Service. Both the stable and preview Services carry `app.kubernetes.io/name`, so
selecting on that would have routed live sessions into preview pods mid-rollout.

Set `sessionAffinity: false` only for a server that genuinely carries full context per
request; that selects `sessionRouting: Stateless` and lets requests spread over all
pods.

### agent: no affinity available, so state must move out of the pod

Agents cannot use the same mechanism. They register with the gateway through an
`HTTPRoute` pointing at a Service, not an `AgentgatewayBackend`, and the backend CRD has
no `a2a` section. Nothing else in the installed stack can pin an A2A session either:
`BackendConfigPolicy` (kgateway) and `BackendLBPolicy` (Gateway API) are both absent,
the installed Gateway API is v1.5.0 **standard** channel with no `sessionPersistence`,
and `AgentgatewayPolicy` has no load-balancing or hash fields.

This matters because agents cache one `Agent` per session in memory
(`app/agent.py` `_agents`). Demonstrated with the same `contextId` against two pods of
the same agent: the first answered `Teal`, the second replied that it had no
information about the preference. The default was `replicas: 3`, so roughly two of every
three follow-ups lost the conversation.

`replicas` is now optional on `agent`, which means one pod unless asked. The durable fix
for scaling agents is not affinity but **external conversation state**: with a memory
provider configured (the `agentcore-memory` trait, `MEMORY_PROVIDER=agentcore`), a
session manager rehydrates state on whichever pod receives the request, and replicas
become free. Set memory first, then scale.

### The deleted kgateway example

`platform/oam/examples/kgateway-session-affinity.yaml` was removed rather than fixed. It
configured `BackendConfigPolicy` from `gateway.kgateway.dev/v1` with RING_HASH and
`X-Context-ID` hash policies, and that CRD is not installed on this platform, so the
file could never be applied. Affinity for MCP now lives in the component definition
where it is on by default, and the agent gap is described above.
