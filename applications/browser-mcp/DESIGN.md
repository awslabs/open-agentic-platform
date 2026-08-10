# Browser MCP: Design State and Pending Decisions

Status as of 2026-08-07. Captures the AgentCore-Browser-as-MCP-server work so it
survives a context compaction. Read this before resuming.

## Objective

Expose Amazon Bedrock AgentCore Browser to agents as an MCP tool through the
platform's agentgateway, instead of compiling browser tooling into the
`strands-agent-base` image (which is what merged PR #43 did, and is being
reverted). Deployable to any region/environment with no change to the OAM.

Data path:
```
agent --(JWT via agentgateway)--> [browser-mcp pod] --(SigV4 / CDP wss)--> AgentCore Browser
```

## Current status (one line)

OAM layer done and regenerated. The **Node-only session-multiplexing browser-mcp
server is implemented and verified end to end against a live AgentCore browser**
(`E2E PASS` + `IDLE PASS`, see Verified results). Remaining work is platform
plumbing: region injection, the PR #43 revert, example app, image publish, commit.

## Verified results (2026-08-07, live AgentCore, us-west-2, `aws.browser.v1`)

`test/e2e-multisession.js` → **E2E PASS**:
1. Boot: `liveBrowserSessions: 0` with `toolsAdvertised: 29`. No browser is
   provisioned at startup, yet the full tool surface is advertised.
2. `tools/list` served to a client while still at 0 live browser sessions.
3. First `tools/call` activated a browser; `navigate_page` + `take_snapshot`
   returned real content (`RootWebArea "Example Domain" url="https://example.com/"`).
   **This is the proof that the hand-ported Node SigV4 WS signing is correct.**
4. A second MCP session ran concurrently: `mcpSessions: 2, liveBrowserSessions: 2`
   on ONE pod, and session A retained its own page state while B navigated
   elsewhere (isolation confirmed).
5. `terminateSession()` (HTTP DELETE) released both; logs show
   `Stopped AgentCore browser session` for each and counters back to 0.

`test/idle-eviction.js` (with `SESSION_IDLE_SECONDS=5`) → **IDLE PASS**:
browser released while idle (`liveBrowserSessions: 0`, `mcpSessions: 1` preserved),
`tools/list` still served, and the next call transparently re-minted a *different*
browser session id (`01KZEDWEJH…` → `01KZEDX16N…`).

`test/abandoned-session.js` (raw HTTP, never sends DELETE) → **ABANDON PASS**:
the reaper released the browser and dropped the session
(`Reaping abandoned MCP session` → `Stopped AgentCore browser session`).

SIGTERM (via `podman stop`) → verified: `Shutting down; releasing browser sessions`
→ `Stopped AgentCore browser session` → `remaining: 0`. Rollouts and scale-downs
do not leak sessions.

**Published image:** `public.ecr.aws/z0a4o2j5/browser-mcp:0.1.0`, multi-arch
(`linux/arm64` + `linux/amd64`), list digest
`sha256:2021175e0ea7f6a8811b9899feea0d0722da33c7411df731d5ba1f944b37897d`.
The published artifact was pulled fresh and re-run: **E2E PASS**.
Repository `browser-mcp` was created in ECR Public (account 929819487611); it is
publicly pullable, consistent with the existing `strands-agent` image there.

Image: `browser-mcp:dev`, Node-only, **184 MB** (was 498 MB with the Python stack),
no Chromium bundled.

Two things found and fixed during validation, worth remembering:
- `client.close()` does NOT terminate the server session; `terminateSession()`
  sends the DELETE. So clients can disappear without releasing anything.
- Therefore the server reaps MCP sessions that go quiet
  (`MCP_SESSION_IDLE_SECONDS`, default 900) in addition to per-session browser
  idle eviction.

## Verified facts (do not re-litigate)

- **Spike PASS (host):** `chrome-devtools-mcp@1.6.0` with `--wsEndpoint`/`--wsHeaders`
  connects to an AgentCore browser session over CDP and `navigate_page` +
  `take_snapshot` return real page content. Lives in
  `spikes/agentcore-browser-mcp-cdp/` with a pinned tool schema `schema-1.6.0.json`
  (29 tools).
- **AgentCore browser model:** a Browser *Tool* (built-in `aws.browser.v1` or a
  custom one) is a shared, long-lived *definition*. *Sessions* are ephemeral and
  isolated (own cookies/state), default 900s, configurable. Multiple sessions per
  Browser Tool run simultaneously. Automation is only exposed as a **CDP
  WebSocket** (`.../browser-streams/<browserId>/sessions/<sessionId>/automation`);
  there is no REST "click/navigate" API. Auth is IAM/SigV4.
- **`generate_ws_headers()`** (Python `bedrock-agentcore` SDK) SigV4-signs the WS
  *upgrade* request (header form: `Authorization`, `X-Amz-Date`, `Host`, upgrade
  headers; `+X-Amz-Security-Token` under temp/Pod-Identity creds). Auth happens
  once at connect; after that the socket lives to the session TTL (CDP frames are
  not re-authorized).
- **chrome-devtools-mcp** uses Puppeteer, connects to a *remote* browser in
  `--wsEndpoint` mode (no local Chromium needed). One instance binds to exactly
  one browser session; its multi-page support is tabs within one session, not
  multiple sessions.
- **In-container (Python image) validations:** image builds browser-less (no
  Chromium; verified), mints the AgentCore session, serves; container egress to
  AgentCore is fine (404 in 0.28s); `chrome-devtools-mcp` lists tools browser-less
  instantly; raw stdio JSON-RPC + real `--wsEndpoint` → initialize + tools/list is
  instant.
- **fastmcp proxy is the blocker:** `FastMCP.as_proxy` (both dict-backend and
  explicit `ProxyClient`/`StdioTransport`) STALLS forwarding `tools/list`. Root
  cause isolated to fastmcp's proxy layer; everything under it works. So fastmcp
  is out.
- **Bridge landscape:** `mcp-proxy` breaks on current `mcp` SDK (import moved).
  `supergateway` (what Docker image `briffa/chrome-devtools-mcp` uses) supports
  `stdio→streamableHttp` (`--stdio`, `--outputTransport streamableHttp`, `--port`,
  `--streamableHttpPath`, `--healthEndpoint`, `--stateful`, `--sessionTimeout`).
  `nullrunner/chrome-mcp-docker` uses a custom Node `index.js`. Neither
  supergateway nor fastmcp does **per-MCP-session backend isolation**; they share
  one backend across sessions.
- **Node feasibility confirmed (npm):** `@aws-sdk/client-bedrock-agentcore`
  3.1105, `@aws-sdk/client-bedrock-agentcore-control` 3.1105,
  `@aws-sdk/signature-v4`, `@aws-sdk/credential-providers`,
  `@modelcontextprotocol/sdk` 1.30 all exist. The MCP TS SDK is the official one,
  ~53M downloads/week, 13k stars, active; it is a thin JSON-RPC/transport layer
  (no formal perf benchmarks; not the bottleneck). Main risk is API churn → pin.

## Decided and implemented (OAM layer)

- **`mcp-server` ComponentDefinition refactored** (`platform/oam/definitions/components/mcp-server.cue`,
  regenerated to `gitops/addons/charts/oam-agent-components/templates/mcp-server.yaml`):
  now self-contained and trait-ready. Creates its own ServiceAccount named
  `context.name`, names its container `context.name`, uses `context.*`, mirrors
  `service-rollout`'s workload params (`image, replicas, port, servicePort,
  command, args, env, healthPath, resources`, blue-green), and keeps the MCP
  outputs (stable/preview Services, `AgentgatewayBackend`, `/mcp/<name>` HTTPRoute,
  optional tool-auth policy). Dropped the old `name`/`namespace` params.
  Verified: `vela def render` (via `platform/oam/generate.sh`, run with
  `KUBECONFIG=.platform/private/hub-kubeconfig` so the cuex compiler resolves)
  regenerates cleanly, only mcp-server changed, no churn to other definitions.
  Example updated: `platform/oam/examples/example-mcp-local.yaml`.
  NOT committed yet (working tree on branch `feature/oam-for-agents`).

- **Decided approach for keeping `mcp-server` a component** (not a trait): accept
  a bounded duplication of the workload skeleton with `service-rollout`, because
  `vela def render` is per-file and does not resolve local CUE imports. True DRY
  would require a cluster-registered `cue.oam.dev` Package (confirmed the CRD
  exists on peeks-hub, currently unused); deferred as not worth the sync-ordering
  dependency for now.

## Session lifecycle and failure semantics (analysed 2026-08-07)

### Two id spaces, joined by an in-memory map

The agent's session id NEVER reaches AgentCore. There is no pass-through.

```
agent conversation
  └─ MCP session id      <- generated by US (randomUUID), returned in Mcp-Session-Id
       └─ sessions.get(sid) -> BrowserSession        [in-memory, pod-local]
            └─ AgentCore session id  <- generated by AWS (ULID)
                 └─ wss://bedrock-agentcore.<region>.amazonaws.com
                      /browser-streams/<browserId>/sessions/<AgentCoreSessionId>/automation
                      (session id is in the PATH, and is covered by the SigV4 signature)
                      └─ baked into that session's own child process argv
```

Isolation comes from the 1:1 chain (one MCP session -> one BrowserSession -> one
AgentCore session -> one child), not from any shared identifier.

Consequences:
- A dead agent session cannot be re-attached. A new agent session gets a new UUID,
  hence a new AgentCore session. The old browser is orphaned, never inherited.
- **The mapping is in-memory and pod-local, so `replicas > 1` breaks routing.**
  `mcp-server` defaults to `replicas: *1` and has no `sessionAffinity`. With more
  than one replica a request can land on a pod that has never seen its session id
  and get HTTP 400. One pod already serves many sessions, so replicas=1 is correct
  today; scaling out would require session affinity at the gateway or Service.
- A pod restart invalidates all MCP session ids; clients must re-initialise.

### Idle vs TTL: different enforcers, so neither supersedes the other

- `SESSION_IDLE_SECONDS` is a `setTimeout` **inside our process**. AWS knows
  nothing about it.
- `sessionTimeoutSeconds` is a TTL **enforced by AgentCore**. AWS docs: default is
  **900s (15 min)** and "sessions automatically terminate after the timeout
  period". No AWS-side *idle* reclamation exists for browser sessions (checked the
  data-plane API model and the docs; idle timeouts documented for AgentCore
  *Runtime* are a different service and do not apply).

Therefore: **on hard pod death the idle timer is guaranteed NOT to fire**, because
the process holding it is gone. The AgentCore TTL is the only backstop. The TTL is
the real safety net; idle is only a cost optimisation for the healthy path.

### What happens on each event

Verified rows were tested this session; the last two follow from documented TTL
behaviour and were not directly exercised.

| Event | Enforced by | Behaviour | Reclaim latency |
|---|---|---|---|
| Client sends `DELETE /mcp` | us | Stop session, drop record | Immediate (verified) |
| No tool calls, pod healthy | us (in-process timer) | Browser stopped, MCP session kept, re-mints on next call | `SESSION_IDLE_SECONDS` (verified) |
| No requests at all | us (reaper) | Stop + drop record | `MCP_SESSION_IDLE_SECONDS` + scan interval (verified) |
| Agent pod dies hard | us, via the idle path | No signal arrives at all; idle reclaims | `SESSION_IDLE_SECONDS` (verified) |
| Our pod gets SIGTERM | us (shutdown handler) | All live sessions released before exit | Immediate, within grace period (verified) |
| **Our pod dies hard** (SIGKILL/OOM/node loss) | **AWS only** | Our timers vanish; nothing of ours runs | **Up to the full TTL from mint** |
| Session reaches TTL while active | AWS (+ our timer at TTL−30s) | We deactivate first; next call re-mints | At TTL |

`test/dying-agent.js` records the agent-death case: a client that activated a
browser then SIGKILLed itself produced **no teardown and no log lines**, with
`mcpSessions=1 liveBrowserSessions=1` still reported at t+40s. Nothing notices a
dead peer, because StreamableHTTP is deliberately connection-independent (a
dropped TCP connection is not a session termination, and the client may hold no
long-lived connection at all).

### Eviction loses browser state (behaviour, not just cost)

Both idle eviction and TTL expiry mean the next tool call gets a **fresh browser**:
cookies, logins and the current page are gone. Fine for "search the web", where
each call navigates anyway. It breaks multi-step flows (log in, then navigate) if
an eviction lands mid-flow. `SESSION_IDLE_SECONDS` only has to cover think-time
between tool calls within a turn (seconds), so 120–300s is generous, but no value
makes state survive a long gap. For real continuity the documented mechanism is
AgentCore **browser profiles** (`profileConfiguration` on `StartBrowserSession`,
persistent cookies and local storage), not longer timeouts.

### Configurability: image defaults, per-team overrides via OAM

Every lifecycle and sizing knob is an environment variable, and `mcp-server`
declares `env: *[] | [...{name, value}]` passed straight to the container. So a
team tunes any of them per application, in git, with no image rebuild and no new
component parameters:

`SESSION_TIMEOUT_SECONDS`, `SESSION_IDLE_SECONDS`, `MCP_SESSION_IDLE_SECONDS`,
`REAPER_INTERVAL_SECONDS`, `MAX_BROWSER_SESSIONS`, `TOOLS_ALLOW`, `TOOLS_DENY`,
`BROWSER_READY_TIMEOUT_SECONDS`, `MCP_PORT`, `MCP_PATH`, `LOG_LEVEL`.

The layering is deliberate: the image carries defaults that are safe for the common
case, the OAM app carries per-team deviations, and enforcement concerns (which
tools a caller may invoke) live at agentgateway via `authPolicy` rather than in
either. `platform/oam/examples/example-browser-mcp.yaml` shows the overrides inline
with the reasoning for each.

### Published image

`public.ecr.aws/z0a4o2j5/browser-mcp:0.1.3` — multi-arch (arm64 + amd64), list
digest `sha256:232bcddee221693976692c2a3f9ad3b0a5cd0c189b7b8d72cbf9def668c4644a`.
Startup logs the effective `sessionTimeoutSeconds` / `idleSeconds` /
`maxBrowserSessions`; verified as `900 / 300 / 25` with no overrides.
`0.1.0` remains available (TTL default 3600).

### Lifecycle changes: decided 2026-08-07

1. **APPLIED:** `SESSION_TIMEOUT_SECONDS` 3600 -> **900**. This is the only bound
   on a hard-death leak, and 900 matches the AgentCore documented default. A longer
   TTL only widens the worst case, since idle fires long before TTL on a healthy
   pod. Startup now logs the effective value.
2. **REJECTED (keep as is):** `SESSION_IDLE_SECONDS` stays at **300**. Shortening
   it would trade browser state (cookies, logins, current page) for a marginally
   smaller orphan window, and 300s of think-time headroom is worth more than the
   saving.
3. **STILL PROPOSED, not implemented:** pod-scoped orphan sweep at boot. We
   currently pass a static `'browser-mcp'` as the AgentCore session `name`. Passing
   `<pod-name>/<mcpSessionId>` would let startup `ListBrowserSessions` and stop only
   sessions bearing THIS pod's name, cleaning up after its own previous
   incarnation. Scoping by pod name is what makes it safe against live peers. This
   is the only mitigation that covers hard pod death, and it also gives CloudTrail
   correlation between the two id spaces.

## On-cluster verification (2026-08-10, peeks-hub, us-west-2)

Deployed via `platform/oam/examples/example-browser-mcp.yaml` with ArgoCD tracking
`feature/agentcore-browser-mcp`. All previously-unverified risks are now closed.

**Crossplane provisioned the custom browser.** The `Browser` managed resource went
`SYNCED=True READY=True` with browserId `agents_web_browser-XZ8Aazris5`. The server
resolved AWS name `agents_web_browser` to that id at runtime, so resolve-by-name
works and no cross-component output wiring was needed.

**Region injection works end to end.** The pod logs `region=us-west-2` with no
region anywhere in the OAM Application. The portability invariant holds in practice,
not just on paper.

**Pod Identity + `accessFor` works.** The pod assumed `browser-mcp-role`, and the
`RolePolicyAttachment` bound `browser-tools-web-browser-iam-policy` to it by naming
convention.

**E2E PASS against the deployed pod** (port-forward, real Pod Identity credentials,
real provisioned browser): 29 tools with 0 browser sessions at boot, `tools/list`
served at 0, `navigate_page` + `take_snapshot` returned real page content, two
concurrent isolated sessions, both released on terminate.

**GATEWAY PASS.** A Keycloak JWT (realm `platform`, role `default-roles-platform`)
was accepted, 29 tools were visible through `/mcp/browser-mcp`, and a `tools/call`
returned real browser content while `liveBrowserSessions` went 0 -> 1.

**GATEWAY ISOLATION PASS.** Two concurrent clients produced **two distinct backend
MCP sessions and two independent browser sessions**, and session A kept its own page
while B navigated elsewhere.

### Finding: agentgateway does NOT pass the MCP session id through

The session id the client sees through the gateway is an opaque agentgateway-issued
token (long base64), not our `randomUUID`. The gateway terminates the MCP session
and maintains its own id, mapped to a backend session.

This matters because the earlier open question ("does agentgateway preserve
`Mcp-Session-Id`?") had the wrong shape. It does not preserve the id, but it does
preserve the **1:1 session mapping**, which is what isolation actually depends on.
Verified by observation on the backend rather than assumed: 2 client sessions
produced exactly 2 backend sessions. Do not rely on correlating a client-visible
session id with a backend one; correlate through the backend's own logs.

### Root cause of the startup restarts (corrected diagnosis)

The first deploy restarted twice with **exit code 137**. My initial reading, that the
`BROWSER_READY_TIMEOUT_SECONDS=300` budget had elapsed, was **wrong**: the last log
was retry attempt 24, about 120 seconds in, so that budget never came close to
expiring.

The real cause: the server did its slow startup work (resolve the browser via AWS,
discover the tool catalog) **before binding the port**, so nothing answered
`/healthz`. The liveness probe runs at `initialDelay 10s, period 30s,
failureThreshold 3`, and there was no `startupProbe`, so the kubelet killed the
container after roughly 100 seconds. IAM propagation for the freshly attached policy
took about five minutes, which meant the pod was killed three times over before its
own retry budget could ever be reached. A larger timeout would have changed nothing.

**Fix (0.1.2):** bind the listener first, initialise in the background.

- `/healthz` answers as soon as the process serves HTTP and stays 200 even when AWS
  is unreachable. Liveness reports that the process is alive, never that a
  dependency is healthy, so a slow or failing dependency cannot restart-loop us.
- `/readyz` returns `503 {"status":"initializing"|"failed"}` until the browser id and
  tool catalog are known, then 200 with counts. Readiness gates traffic.
- MCP handlers return a clear "still initialising" error instead of throwing.
- `mcp-server` gained a **`readinessPath`** parameter (defaults to `healthPath`).
  Both probes previously shared one path, so an always-200 `/healthz` would have
  made the pod Ready before it could serve. Any server with slow startup work needs
  the two paths separated.

Verified by running the image with **no credentials at all**, so initialisation
cannot succeed: `/healthz` returned 200 within 2s while `/readyz` reported
`503 {"status":"initializing"}`.

Re-verified on-cluster with 0.1.2: pod shows `live=/healthz ready=/readyz`,
**0 restarts**, and logs the intended ordering (`listening; initialising (not ready
yet)` then `ready ... initSeconds=1`). E2E PASS, GATEWAY PASS and GATEWAY ISOLATION
PASS all re-run against the redeployed pod.

Generalisable lesson for other platform workloads: a liveness probe must not depend
on an external dependency being reachable, and a workload that must call AWS before
it can serve should bind its port first or declare a `startupProbe`.

### Clean-state redeploy found a second bug: terminal unready (0.1.3)

Deleting the OAM Application and redeploying reproduced the original conditions
exactly, because deleting it also deletes the IAM policy and role attachment, so
re-applying re-triggers propagation. That test found a flaw local testing could not.

0.1.2's liveness fix worked: the pod was alive and retrying at **264 seconds**, where
0.1.1 was killed at ~100s, with 0 restarts. But when the
`BROWSER_READY_TIMEOUT_SECONDS` budget expired, initialisation logged "staying
unready" and **stopped trying**. The pod then sat Running and permanently NotReady
until a human deleted it. Everything it needed was healthy by then (browser
`True/True`, policy `True/True`, attachment `True/True`), so a retry would have
succeeded immediately.

That is worse than the crash loop it replaced: a crashing pod at least retries. It
also violates the platform's customer bar, which requires convergence without human
intervention.

**Fix (0.1.3):** initialisation retries in an outer loop with **no terminal state**.
Each cycle still retries internally for `BROWSER_READY_TIMEOUT_SECONDS`, then pauses
`INIT_RETRY_SECONDS` (default 15) and starts over, forever. Readiness gates traffic
and liveness only reports that the process is up, so retrying indefinitely is safe
and converges whenever the dependency appears. `/readyz` reports `lastError` and
`failedCycles` instead of a `failed` status, because there is no giving up.

Also fixed a misleading error that cost real diagnosis time: resolution failure
always reported `browser "X" not READY within 300s` even when the cause was
`AccessDenied` during IAM propagation. The underlying cause is now carried into the
message.

Verified locally against a nonexistent browser with a 10s budget: three failed cycles
and still retrying, `/healthz` 200 throughout, `/readyz` reporting
`{"status":"initializing","failedCycles":3,"lastError":"...not present in
ListBrowsers yet"}`. Redeployed clean on-cluster: ready in 1s with `cycles: 1`,
0 restarts, and E2E / GATEWAY / GATEWAY ISOLATION all passing again.

Caveat on what is NOT yet proven on-cluster: the 0.1.3 deploy reused the existing IAM
policy, so it did not sit through a fresh propagation delay. The self-healing path is
proven locally, not against a real multi-minute AWS delay. Deleting the Application
and redeploying 0.1.3 would close that gap.

Lesson worth applying to other platform workloads: liveness must not depend on a
dependency being reachable, AND initialisation must never give up. Those two together
are what make a workload self-healing; either alone leaves a hole.

### Two operational notes

**IAM propagation took roughly 5 minutes.** The pod logged repeated
`AccessDenied ... not authorized to perform: bedrock-agentcore:ListBrowsers` while
the freshly-attached policy propagated, then recovered with no intervention. This is
the window `aws-service-identity`'s comment says to handle with app-level retry, and
our retry loop does. Note this is a first-deploy cost only: it does not recur once
the policy exists. What it exposed was the probe bug above, not a timeout that needs
raising.

**`kubectl get application` is ambiguous on this cluster.** ArgoCD and OAM both
register an `application` kind, and the short name resolves to ArgoCD's. Use
`kubectl get applications.core.oam.dev <name> -n <ns>` for OAM apps.

## Design decisions (implementation status marked per item)

Region injection and the example app are still TODO. The browser-mcp server
design below is IMPLEMENTED and verified.

- **Region is environment config**, injected by the platform, never in the OAM.
  Mechanism: `global.awsRegion` (sourced per-cluster from the cluster secret
  `aws_region` annotation via `gitops/addons/bootstrap/default/addons.yaml`),
  baked into generated definitions at chart render, as
  `decentralized-observability-identity` already does. TODO: add `AWS_REGION`
  injection to the `aws-service-identity` trait so any workload with the trait
  gets its cluster's region ambiently; and align `agentcore-browser` to source
  region from `global.awsRegion` (drop its region param).

- **Browser name is application config**, user-provided, passed explicitly to
  both the `agentcore-browser` component (`browserName`, must match
  `[a-zA-Z][a-zA-Z0-9_]*`, no hyphens) and the browser-mcp server env
  (`AGENTCORE_BROWSER_NAME`). Duplicating it in the app is acceptable and makes
  switching to a different provisioned browser trivial. The broker resolves
  name→browserId at runtime via `ListBrowsers` (or uses a built-in id like
  `aws.browser.v1` directly). No ConfigMap, no cross-component output wiring.

- **Target OAM app shape** (region-free, portable):
  ```yaml
  components:
    - name: web-browser
      type: agentcore-browser
      properties: { browserName: agents_web_browser, networkMode: PUBLIC }
    - name: browser-mcp
      type: mcp-server
      dependsOn: [web-browser]
      properties:
        image: <browser-mcp image>
        port: 8000
        env: [{ name: AGENTCORE_BROWSER_NAME, value: agents_web_browser }]
      traits:
        - type: aws-service-identity
          properties: { accessFor: [web-browser] }
  ```

- **browser-mcp server = Node-only, session-multiplexing MCP server** (the pivot):
  - One Node app, one StreamableHTTP MCP endpoint (`/mcp`), pinned
    `@modelcontextprotocol/sdk`.
  - **Per MCP session** (`Mcp-Session-Id`): its own AgentCore browser session and
    its own dedicated browser-less `chrome-devtools-mcp` child, so conversations
    never share cookies or page state. Routing is keyed by MCP session id.
  - This multiplexer is ours to write; no off-the-shelf bridge (supergateway,
    fastmcp) does per-session backend isolation.
  - The pod is a thin **signer/router**: it holds no browser. Concurrency is
    bounded by AgentCore session limits and our per-pod cap, NOT by pod capacity.
    **Do not scale pods to get more browser sessions**; a single pod hosts many.
    Children are browser-less (≈ tens of MB, ~0 idle CPU).

- **Lazy activation (binding requirement).** No browser session is started at pod
  start, at MCP `initialize`, or for `tools/list`. A browser session is minted only
  on the **first actual `tools/call`** of a given MCP session. Rationale: the agent
  must be able to *see and reason about* the browser tools (and choose to use them
  for e.g. a web search) without anything being provisioned or billed until it
  actually browses.
  - **Tool discovery without a browser:** spawn one browser-less
    `chrome-devtools-mcp` at boot purely to read its tool list, cache it, and let it
    exit; serve `tools/list` from that cache thereafter (proven: browser-less
    `tools/list` returns all 29 tools instantly). Static
    `spikes/.../schema-1.6.0.json` is the fallback / drift reference. This keeps the
    advertised schema self-maintaining against the pinned version with zero idle
    processes and zero AgentCore cost.
  - **Lifecycle per MCP session:** first `tools/call` → resolve browser id → mint
    session → SigV4-sign CDP endpoint → spawn child → forward call. Subsequent calls
    reuse it.
  - **Idle eviction:** after `SESSION_IDLE_SECONDS` with no calls, stop the
    AgentCore session and kill the child, while continuing to serve `tools/list`.
    A later call transparently re-mints. This makes an idle pod effectively free.
  - **TTL handling is per session** (each has its own deadline), replacing the old
    crude whole-pod recycle. On MCP session close, or TTL, or shutdown:
    `StopBrowserSession` + kill child.

## Pending decisions (need the user)

RESOLVED 2026-08-07: (1) Node-only session-multiplexing rewrite = GO.
(2) Bridge = in-app with `@modelcontextprotocol/sdk` (no supergateway child).
(3) **Tool surface = advertise all 29.** Restriction is a deployment concern, not
an image concern: use the `mcp-server` component's `authPolicy` (CEL, enforced by
agentgateway on the backend) when access must be limited, and `TOOLS_ALLOW`/
`TOOLS_DENY` if the agent should not even see certain tools.
Plus: lazy activation and many-sessions-per-pod are binding (see above);
scaling pods to add browser sessions is explicitly rejected.

1. **Session-to-conversation mapping:** confirm the strands agent opens a distinct
   MCP session per conversation (needed for the per-conversation isolation the
   multiplexer provides). Also confirm agentgateway preserves `Mcp-Session-Id`
   end to end.
2. **Per-pod concurrency cap + AgentCore session quota**: default is
   `MAX_BROWSER_SESSIONS=25`; confirm the account's AgentCore browser session
   quota so the cap sits under it.

## Open risks

CLOSED 2026-08-10 by on-cluster verification (see above): Node SigV4 signing,
Crossplane provisioning of a custom browser, region injection, `accessFor` policy
attachment, and agentgateway session handling including two-client isolation.

Remaining:
- **MCP TS SDK API churn.** Pinned to 1.30.0; 79 releases in ~20 months.
- **`replicas: 1` only.** Session state is in-memory and pod-local, so scaling out
  requires session affinity first. Not a capacity limit: one pod serves many
  browser sessions.
- **First-deploy IAM propagation** was observed at roughly 5 minutes, during which
  the server retries and is correctly not-ready. Fixed the restart loop it exposed
  (0.1.2); the retry budget itself was never the constraint.
- **Resource sizing** not tuned: no requests/limits set on the example.
- **IAM policy is a wildcard** (`bedrock-agentcore:*` on `*`) inherited from the
  original AgentCore components. Should be scoped to the browser ARN.

## Still-open platform items (separate from the broker)

- Revert PR #43's base-image pollution in `strands-agent-base` (`_get_agentcore_tools`
  in `app/agent.py`, `CODE_INTERPRETER_*`/`BROWSER_*` in `app/config.py`,
  `strands-agents-tools` dep, `browser`/`codeInterpreter` params + `_toolsEnv` in
  `agent.cue`, the placeholder examples), reconciled with open PR #36 which also
  edits `agent.py`. Keep the `agentcore-browser` Crossplane ComponentDefinition
  (add its missing `.cue` source so it stops being an orphaned generated file).
- Add `AWS_REGION` injection to `aws-service-identity`; align `agentcore-browser`
  region to `global.awsRegion`.
- Add the browser-mcp example app.

## Where things live

- Spike (proven CDP path, pinned schema): `spikes/agentcore-browser-mcp-cdp/`
  (untracked; has `README.md` with the recorded PASS, `mint_session.py`,
  `run_spike.py`, `capture_schema.py`, `schema-1.6.0.json`).
- browser-mcp server (Node, implemented): `applications/browser-mcp/`
  - `src/server.js` — StreamableHTTP endpoint, per-MCP-session Server + routing,
    concurrency limiter, health endpoints, abandoned-session reaper, graceful stop.
  - `src/session.js` — `BrowserSession`: lazy activation, idle eviction, per-session
    TTL, teardown. Constructing one provisions nothing.
  - `src/agentcore.js` — browser resolution, session start/stop, and the SigV4
    WS-upgrade signing ported from the Python SDK reference.
  - `src/catalog.js` — boot-time browser-less tool discovery + allow/deny filter.
  - `src/config.js`, `src/log.js`, `Dockerfile` (node:20-alpine, pinned by digest),
    `package.json` (all deps pinned).
  - `test/e2e-multisession.js`, `test/idle-eviction.js`,
    `test/abandoned-session.js`, `test/dying-agent.js` — self-timeout harnesses;
    none of them can hang.
  - The Python `broker.py`/`requirements.txt` prototype has been deleted.
- OAM: `platform/oam/definitions/components/mcp-server.cue` (+ generated yaml),
  `platform/oam/examples/example-mcp-local.yaml`,
  `platform/oam/definitions/traits/aws-service-identity.cue`.

## Verification method (use this; it does not hang)

Never attach the shell to the long-running server. Run detached, poll bounded,
read logs, use a self-timeout client:
- `podman run -d ...` then a bounded readiness loop (`curl -m 2 ... /mcp`), then
  `podman logs`.
- Client with an internal `asyncio.wait_for`/timeout so it self-terminates.
- Wrap steps in `perl -e 'alarm N; exec @ARGV' ...` as a hard ceiling (macOS has
  no `timeout`).
- Creds for local runs: `eval "$(aws configure export-credentials --format env)"`,
  pass via `-e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY [-e AWS_SESSION_TOKEN]`.
- Use `AGENTCORE_BROWSER_NAME=aws.browser.v1` (built-in) to avoid needing a
  Crossplane-provisioned browser for local tests.

## Next steps (resume here)

1. **Pod-scoped boot orphan sweep** (the one lifecycle item left; see above).
   Verify by SIGKILLing the container so a session leaks, then restarting and
   confirming the orphan is stopped.
2. **Decide replica policy:** either keep `replicas: 1` (correct today, since one
   pod serves many sessions) or add session affinity before anyone scales out.
3. **Platform plumbing:** inject `AWS_REGION` from `global.awsRegion` in
   `aws-service-identity.cue`; align `agentcore-browser` region to
   `global.awsRegion` and add its missing `.cue` source; regenerate with
   `KUBECONFIG=.platform/private/hub-kubeconfig ./generate.sh`.
4. **`platform/oam/examples/example-browser-mcp.yaml` — WRITTEN.** Uses
   `browser-mcp:0.1.1`, `healthPath: /healthz`, `replicas: 1`, and shows per-team
   env overrides. It deliberately contains NO region, so it depends on item 3
   (trait region injection) to run as written; until then a deployment needs
   `AWS_REGION` supplied some other way.
5. **Revert PR #43's base-image pollution** in `strands-agent-base`, reconciled
   with PR #36's changes to the same `agent.py`.
6. **Verify on cluster:** MCP session id preserved through agentgateway, and that
   the strands agent opens a distinct MCP session per conversation. If the agent
   reuses ONE MCP session across conversations, they all share a single browser and
   the isolation above is not delivered despite the server supporting it.
7. **Commit** the whole set to `feature/oam-for-agents` (mcp-server.cue refactor is
   still uncommitted). Commit only when asked; do not push to main.

DONE: Node server implemented and verified; all four resource-release paths
verified; tool surface decided (all 29, restrict via agentgateway `authPolicy`);
multi-arch image published and re-verified from the registry.
