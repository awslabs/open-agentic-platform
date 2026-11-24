# Agent guardrails: setup and operation guide
This guide explains how the OAP agent guardrails work and how to adopt them. It has two
audiences:
- **Agent teams** who want their agent's model calls screened for unsafe content
  (sections 1, 2, 4, 5).
- **Platform operators** who enable and run guardrails on a cluster (sections 1, 3, 4, 5).
The design, the decisions taken and the upstream facts behind them are in
[docs/architecture/agent-guardrails.md](../architecture/agent-guardrails.md), section 9
in particular. Statements marked **Not verified** were not exercised on a cluster. Phase 1
was checked with chart renders, CUE renders, unit tests and `promtool` rule tests only.

## 1. What it is and when to use it
With guardrails on, an agent's model calls go to the agent gateway instead of straight to
Bifrost. The gateway sends each request to Amazon Bedrock `ApplyGuardrail` and, if the
request violates the agent's profile, refuses it before the model sees it. Bifrost records
no call for a refused request.

```
agent -> agentgateway (/llm/<profile>, promptGuard) -> Bifrost -> Bedrock
```

| | |
|---|---|
| What is checked | the request side only, in Phase 1: user and assistant messages and tool results (`scope: [Messages, ToolOutput]`). Responses are not checked and streaming is not affected |
| Who decides what a profile detects | the platform maintainers, in the profile catalog. An agent team picks a profile name |
| Default | off. The `guardrails` block and the cluster flag are both opt-in |
| Cost | one `ApplyGuardrail` call per model call, not sampled. See section 3.5 |

Use it when an agent takes input you do not control (users, tools, retrieved documents) and
you need a platform-enforced answer to "was this prompt acceptable?". It does not authorize
tool calls and it does not inspect model output.

## 2. For agent teams
### 2.1 Turn it on
Add a `guardrails` block to your `agent` component. This is the whole change on your side
([example](../../platform/oam/examples/example-agent-guardrails.yaml)):

```yaml
spec:
  components:
    - name: my-agent
      type: agent
      properties:
        # ... your existing properties ...
        guardrails:
          enabled: true
          profile: standard   # observe | standard | strict, as the platform defines them
```

| Field | Default | Validation (CUE at admission) |
|---|---|---|
| `enabled` | `false` | bool. `false` or no block renders nothing, except in a namespace the platform requires guardrails for, where it is rejected |
| `profile` | `standard` | must be a profile the platform lists. It can be raised and never lowered below the namespace minimum |

There is no threshold, tool list or `onViolation` key. The profile owns what is detected.
With guardrails on, `modelConfig.llmGatewayUrl` is ignored: the model URL becomes
`http://agentgateway-proxy.<gateway-namespace>.svc.cluster.local:8080/llm/<profile>/v1`.

Prerequisites: the cluster runs agent guardrails (section 3.1) and your agent image is
v1.8.0 or later. An older image sends no token on model calls and the gateway answers 401.

### 2.2 Profiles
| Profile | Meaning in the starter catalog | Who may use it |
|---|---|---|
| `observe` | content filters at LOW, recorded and never rejected (`Audit`) | only agents the platform lists in `guardrailAccess` |
| `standard` | content filters at MEDIUM, prompt attack detection, PII anonymized | every agent |
| `strict` | content filters and prompt attack at HIGH, PII anonymized, one example denied topic | every agent |

The starter catalog is a working default. The platform maintainers own the final contents.
Ask your platform operator what a profile detects on your cluster.

### 2.3 What a refusal looks like
| Situation | What the agent gets |
|---|---|
| The request violates the profile | HTTP 403 with the platform's rejection message ("The request was blocked by the platform guardrail." by default). Bifrost records no call |
| The guard could not decide (Bedrock throttled or unreachable, or the guardrail is not bound yet) | a failed request, HTTP 503 per the v1.7.0 source. Every profile fails closed, so the request is refused rather than sent unchecked. **Not verified** on a cluster |
| No token, or a token the gateway does not accept | HTTP 401 |
| `observe` without a grant | HTTP 403 from the route's access rule |
| A profile that masks PII | the request reaches the model with the matched text replaced |

### 2.4 The token
With guardrails on, the `agent` component mounts its projected `agentgateway`
ServiceAccount token and sets `LLM_GATEWAY_AUTH_MODE=workload-token`. The v1.8.0 image sends
the token as the bearer on every model call and re-reads the file, so a rotated token is
picked up without a restart. You do not add the `gateway-identity` trait for this.

### 2.5 `observe` grants
`observe` records findings without rejecting. Because it is weaker than the default, an
agent may use it only when the platform lists it, in the environment's
`gitops/overlays/environments/<env>/agent-gateway/values.yaml`:

```yaml
guardrailAccess:
  observe:
    agents:
      - my-namespace/my-agent   # <namespace>/<agent>, a bare name fails the render
```

### 2.6 See what is happening
The Agent Guardrails dashboard in Grafana shows outcomes by profile and by agent, refusals
caused by guard failures, latency and the configuration in use. Series are
`oap_agent_guardrail_*` with `hop="model"`. An agent with no traffic has no series. A
violation is a product signal for you. A guard failure (`error_blocked`) is a platform
problem, not a sign your agent misbehaved.

## 3. For platform operators
### 3.1 Enable the flag per environment
In `gitops/overlays/environments/<env>/enabled-addons.yaml`:

```yaml
enabledAddons:
  agent_guardrails: true
  agent_gateway: true     # required
  agent_core: true        # required
  oam_components: true    # the agent component that carries the block
```

The flag becomes the `enable_agent_guardrails` cluster-secret label. One registry expression
turns it into `guardrails.enabled` on `agent-gateway`, `crossplane-agentcore` and
`oam-agent-components`, and it is true only when `enable_agent_gateway` and
`enable_agent_core` are also true. A test pins that the three agree. It is `false` in every
overlay by default. With the flag off nothing guardrail-related renders and an agent that
sets `enabled: true` is rejected at admission with a message naming the flag.

For per-agent outcomes the collector also needs `enable_otel_collector`. See section 3.6.

### 3.2 The profile catalog and who owns it
Two charts name the same profiles in the same order, and a test fails if they differ:

| Chart | Holds | Values |
|---|---|---|
| `crossplane-agentcore` | what each profile detects (content filters, PII, denied topics), the tier, the catalog revision | `guardrails.profiles`, `guardrails.order`, `guardrails.catalogRevision` |
| `agent-gateway` | how the gateway enforces it (action, failureMode, scope), the default profile, grants | `guardrails.profiles`, `guardrails.order`, `guardrails.defaultProfile`, `guardrailAccess` |

The platform maintainers own the contents. A `GuardrailVersion` is an immutable snapshot, so
raise `catalogRevision` in the same change as any edit to a profile. Crossplane then cuts a
new version and the binder writes it into the gateway policy.

`oam-agent-components` takes `global.guardrails.defaultProfile` and `global.guardrails.order`
(the same values as the gateway) and `global.guardrails.requiredNamespaces`
(`team-payments=strict,team-b=standard`), which makes guardrails mandatory in a namespace with
a minimum profile.

### 3.3 Image build and push
Agents need `strands-agent-base` v1.8.0 or later. `global.agentImage` in
`oam-agent-components` points at the v1.8.0 tag, which does not exist in the registry until
someone builds and pushes it. Build and push it with the steps in
[applications/strands-agent-base/README.md](../../applications/strands-agent-base/README.md)
before enabling the flag on a shared cluster. Do not merge the image bump before the push.

### 3.4 Required platform pieces
With the flag on the charts render these, with nothing hardcoded (account, Region and
cluster name come from cluster-secret annotations):

| Piece | Where |
|---|---|
| `provider-aws-bedrock` (v2.9.0 or later, first with `GuardrailVersion`), its ServiceAccount, `DeploymentRuntimeConfig`, Pod Identity role and association | `crossplane-agentcore` |
| `Guardrail` and `GuardrailVersion` per profile, tagged with the cluster | `crossplane-agentcore` |
| IAM role and Pod Identity association for the gateway proxy ServiceAccount, allowing only `bedrock:ApplyGuardrail` on `guardrail/*` in the account and Region with a cluster tag condition, plus the cross-Region guardrail profile | `agent-gateway` |
| The `llm` AI backend (custom provider, `Completions`, aimed at Bifrost), one `HTTPRoute` and access policy per profile, one `promptGuard` policy per profile | `agent-gateway` |
| The guardrail binder: a PostSync Job and a repair CronJob that write each guardrail's generated ID and version into its policy | `agent-gateway` |
| A ClusterIP metrics Service for the proxy, scraped on port 15020 | `agent-gateway` |

The policies render with the identifier `pending`, which fails closed. Until the binder has
run, every request on a profile is refused. Check the binder Job if you see that.

**Not verified:** that the shared ProviderConfig `default` of the v2.7.0 provider family works
with the v2.10.0 `provider-aws-bedrock` package. If a cluster rejects the pairing, raise the
family version in appmod-blueprints to the same tag.

### 3.5 Standard tier, cross-Region profile, quotas and cost
The catalog uses the Standard tier, which needs a cross-Region guardrail profile
(`guardrails.crossRegion.profileId`, for example `us.guardrail.v1:0` for the US Regions). Bedrock
may route an evaluation to another Region of the same geography. Both charts carry the same
value and a test pins that. Where cross-Region routing is not allowed, set `tier: CLASSIC` and
an empty `profileId`. Classic has weaker detection and a lower denied-topics quota (50 text
units per second against 200 on Standard).

Quotas are per account and Region, shared by every cluster in it. In the US West (Oregon)
Region on the Standard tier, content filters and denied topics allow 200 text units per
second with a burst of 1,000. A 20,000-character request is 20 text units, so about 10 guarded
model calls per second across all clusters. All quotas are adjustable.

Cost is $0.15 per 1,000 text units for content filters and denied topics and $0.10 for PII,
one text unit per 1,000 characters, charged per model call on the text the profile's `scope`
selects. A multi-turn call re-sends the conversation, so cost grows with the number of turns.
The measured text units per call, latency and quota behaviour are a live test item and have
not been measured yet.

### 3.6 Observability and alerts
Per-agent outcomes cannot come from the gateway's own guardrail counter, which has only
`phase` and `action`. They come from the gateway access log:

1. A frontend `AgentgatewayPolicy` on the Gateway exports one OTLP log record per `/llm`
   request, with only `agent`, `route`, `status`, `reason`, `guardrail` and
   `guardrail_version`. No prompt text, completion or guard response body is exported.
2. The OTel collector counts the records and writes the counters to AMP. The collector that
   writes to AMP runs a pinned OpenTelemetry Collector contrib image while the flag is on,
   because the ADOT build does not include the count connector.
3. Recording rules turn the counters and the proxy metrics into `oap_agent_guardrail_*`.

Both `agent-gateway` and `otel-collector` take `guardrails.accessLog.enabled`, derived in
the registry from the same labels. The gateway side also needs `enable_otel_collector`.

| Rendered object | Needs |
|---|---|
| `PrometheusRule` (rules and alerts) | `guardrails.alerts.enabled` and the `monitoring.coreos.com/v1` CRD |
| `GrafanaDashboard` | `guardrails.grafana.enabled` and the `grafana.integreatly.org/v1beta1` CRD |

Without the CRD the object is not rendered and the rest of the chart still syncs.

Alerts, all about the platform and none about a rising block rate:

| Alert | Fires when |
|---|---|
| `GuardrailFailClosedRefusals` | `error_blocked` per second is above `guardrails.alerts.failClosedRatePerSecond` for 5 minutes. The guard failed and the request was refused |
| `GuardrailFailOpenFired` | a request went through unchecked because a profile fails open. Profiles in `guardrails.alerts.deliberateFailOpen` are left out |
| `GuardrailDataStale` | requests reach `/llm` and no guard decision has been counted for `guardrails.alerts.dataAgeSeconds` |
| `GuardrailConfigSkew` | more than one guardrail version checked one profile for over 15 minutes |

`error_blocked` is derived from a 503 with reason `Internal` and no guard action, because
agentgateway v1.7.0 has no `FailClosed` metric action. Another internal fault on an `/llm`
route is counted the same way.

### 3.7 Rollback
Set `agent_guardrails: false` for the environment. The flag turns off all three charts and
the collector pipeline. Agents that still set `guardrails.enabled: true` are then rejected at
the next render, so remove the block (or the namespace requirement) in the same change. Agents
already running keep their old `LLM_GATEWAY_URL` until they re-render, and the route is gone
by then, so roll the agents in the same window. Existing Bedrock guardrails stay in the
account until the `Guardrail` objects are deleted.

## 4. Behaviour you can rely on
- **Fail closed.** Every guard sets `failureMode: FailClosed` on every profile. If Bedrock is
  throttled, unreachable or the guardrail is not bound, the request is refused. A profile
  fails open only if an operator sets `FailOpen` in values, and the alert says so.
- **`/llm` is cluster-internal.** Each route matches only the Gateway's in-cluster hostname, so
  a request with the public agents hostname matches no route, and the agents Ingress answers
  `/llm` with a 404 before it reaches the proxy.
- **Request side only in Phase 1.** Responses are not inspected. Response guards and the
  streaming trade-off are Phase 2.
- **An agent can still bypass by hand.** It can set `llmGatewayUrl` to Bifrost until a
  default-deny egress policy or mandatory virtual keys land in Phase 2.
- **No content in logs or labels.** The access log export is an allowlist of metadata.

## 5. Troubleshooting
| Symptom | Cause and fix |
|---|---|
| 401 on `/llm` | the agent sent no token. Check the image is v1.8.0 or later and `LLM_GATEWAY_AUTH_MODE=workload-token` is set on the pod |
| 403 on the `observe` route | the agent has no grant. Add it to `guardrailAccess.observe.agents` as `<namespace>/<agent>` |
| 403 on `standard` or `strict` | the guard rejected the request (the body is the platform's rejection message). A 403 with no message is the access rule, so check the agent's ServiceAccount token |
| 503 on every request | `identifier: pending` means the binder has not run. Check the `guardrail-binder` Job in the namespace the `agent-gateway` app installs to and the `Guardrail` status. Otherwise check Bedrock throttling and the gateway logs |
| The agent is rejected at admission with a flag message | `agent_guardrails` is not on for this cluster. See section 3.1 |
| The agent is rejected as below the minimum | raise `profile` to the namespace minimum named in the message |
| No per-agent series | check `guardrails.accessLog.enabled` on both charts, the `otel-collector-guardrail` Service and the collector pipeline, in that order |
| `agentgateway-proxy-metrics` has no targets | the metrics Service selects the proxy pods by the Gateway name. Check the labels on the proxy pod |
