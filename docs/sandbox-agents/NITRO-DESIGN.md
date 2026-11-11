# Nitro Sandboxes as the default isolation runtime, and per-user isolation for agents

> **Status:** implemented; rendering and admission behaviour verified offline and against a live
> EKS 1.37 Auto Mode hub. Runtime verification on a sandbox-capable node is the outstanding item
> (§9).
>
> **Two changes in one design**, because they share a mechanism and would otherwise be written
> twice:
>
> 1. **The isolation runtime becomes AWS Nitro Sandboxes** (`isolated-sandbox`) instead of Kata,
>    and a second trust model (`confidential-sandbox`) becomes available to developers. Kata stays
>    fully supported, selected per cluster.
> 2. **Isolation becomes expressible per user session** (`isolation: shared | tools | session`),
>    which is [issue #99](https://github.com/awslabs/open-agentic-platform/issues/99) — a developer
>    declares an agent once and the platform hands out per-session instances.
>
> This builds on [`DESIGN.md`](DESIGN.md), the Kata design that introduced `sandbox: true`. Nothing
> in that document is retracted: its mechanism (one field on the pod template, the RuntimeClass
> admission controller doing the placement) is exactly what made swapping the runtime a
> one-value change.

## 1. Why change the default at all

`sandbox: true` works and is verified on Kata. The problem is what it costs to *have* a cluster it
works on:

| | Kata | Nitro Sandboxes |
|---|---|---|
| Node layer | A self-managed Karpenter pool of **nested-virt** instances (`kata_nodepool`) | None of ours. On EKS Auto Mode a sandbox-capable node is provisioned on demand |
| Runtime install | `kata-deploy` DaemonSet per VMM (`agent_sandbox_kata`, and a second release for Firecracker) | None. The runtime is in the AMI |
| RuntimeClasses | **This repo renders them** (`agent-sandbox/templates/10-runtimeclasses.yaml`), because `kata-deploy` cannot express their tolerations | EKS owns them; we select by name |
| State in this repo today | `kata_nodepool: false` and `agent_sandbox_kata: false` in **every** overlay | — |
| Confidential computing | No equivalent | `confidential-sandbox` |
| Attestation | None | Nitro Secure Module PCRs, verifiable by AWS KMS |

The fourth row is the decisive one. On a stock cluster from this repo, `sandbox: true` with the old
default named `kata-clh`, no node carried `katacontainers.io/kata-runtime`, and the pod sat
`Pending` forever. The isolation was one `enabled-addons.yaml` edit plus a node pool away — which is
fine for a platform team and not fine as a default.

Nitro also brings something Kata cannot: a trust model where the **node operator is outside** the
boundary, with hardware attestation to prove it. That is a different security property, not a faster
VMM, and it is what makes `sandboxMode` worth adding as a developer-facing field.

## 2. Contract

Three fields, all opt-in, all declared once per agent. The first already existed.

```yaml
- name: data-analyst
  type: agent
  properties:
    sandbox: true                 # hardware isolation, as before
    sandboxMode: confidential     # NEW: trust model. isolated (default) | confidential
    isolation: session            # NEW: scope.      shared (default) | session
    sandboxTemplate: analyst-sbx  # NEW: which sandbox the per-session tiers claim from
```

| Field | What it answers | Who owns it |
|---|---|---|
| `sandbox` | Is there a hardware boundary at all? | developer |
| `sandboxMode` | **Who is inside** the boundary — is the node trusted? | developer |
| `isolation` | **How many** boundaries — one per agent, or one per user session? | developer |
| *(the RuntimeClass)* | Which technology implements the boundary | **platform**, per cluster |

That last row is the line this design holds. `.kiro/steering/oam-authoring.md` §1 forbids ambient
environment values in a developer's OAM, and a RuntimeClass name is one: it depends on the cluster's
Kubernetes version, its node layer and its region. A **trust model** is not — it is a property of
the workload ("my data must survive an untrusted node operator"), true on every cluster the
Application could deploy to. So the developer states the model and the platform maps it to a class.

### 2.1 What each value does

| | `isolated` (default) | `confidential` |
|---|---|---|
| Node is | trusted | **untrusted** |
| Protects | the node and other pods, from the workload | the workload, from the node operator |
| `kubectl exec`, stdin | allowed | **denied** |
| Logs | allowed | allowed |
| Overlay filesystem | plaintext (faster start) | encrypted |
| Container admission | not pinned | **pinned**: image digest, args, env, mounts, capabilities, security settings |
| Cedar policy | optional | **required** |
| Attestation | measured | measured, and verifiable by a KMS key policy |
| RuntimeClass overhead | 256Mi + 1 sandbox slot | 512Mi + 1 sandbox slot |

| | `shared` (default) | `session` |
|---|---|---|
| Agent process | one, for everyone | one front door |
| Per-session sandbox | none | a dedicated instance per `(agent, user, session)` |
| Cost | lowest | highest |
| Good for | Q&A, retrieval, read-only tools | regulated or multi-tenant work, long-lived per-user state, an AgentCore-Runtime-equivalent guarantee on EKS |

### 2.2 Why there is no `tools` tier

#99 proposes three tiers, with `tools` — "shared agent loop, per-session code execution" — as the
recommended first step. **This design implements two**, and the omission is deliberate rather than
deferred work.

The capability is real and wanted. The objection is that the agent component is the wrong place to
express it. `shared` and `session` answer *how many agent instances exist per user*, which is a
property of the agent, so this component owns it. "Where does code execution run" is a property of
the **tool server**: an agent just has an MCP server wired, and whether that server executes code in
AgentCore or in a claimed sandbox is the server's own configuration. A field here would duplicate a
decision that belongs one layer down — the same leak steering §1 forbids for region and account.

It is also redundant either way it could be wired:

| Tool backend | What `isolation: tools` would add |
|---|---|
| AgentCore Code Interpreter (`code-interpreter-mcp` today) | Nothing. AgentCore already isolates each session, off-cluster |
| An on-EKS sandbox backend | Nothing new to declare. The per-session sandbox *is* the tool backend, so the "tier" is `code-interpreter-mcp` pointed at a `type: sandbox` pool — one config value there, plus a pool the developer declares anyway |

So per-session tool isolation is delivered by **declaring a `type: sandbox` pool and pointing an MCP
server at it, with the agent left on `shared`** — which is one more component and no new agent
surface. Earlier drafts of this design shipped the tier and called it the recommended first step.
That was wrong twice over: it was the only tier that could not work with what ships here, and
recommending it made the whole feature look dependent on a code interpreter it does not need.

If the one-word shortcut is later asked for, it can return as sugar over that wiring — once the
backend exists. Raising it on #99 rather than narrowing the issue silently is part of this change.

**What works today, with nothing else built, is the sandbox claimed directly** — a caller creates a
`SandboxClaim`, the operator binds a warm instance, the caller drives it and deletes the claim. That
needs no MCP server, no code interpreter and no agent image change, and the Dark Factory has run
exactly that shape on this platform since V2. Order of adoption, therefore: `type: sandbox` claimed
by an orchestrator or your own service first; `isolation: session` when its agent-side router lands.

## 3. Mechanism

Unchanged from the Kata design, which is the point. One field on the pod template:

```
 OAM Application                  agent.cue
   sandbox: true       ────────►  _isolationClass ◄── platform, per cluster
   sandboxMode: X                     │
                                      ├─ isolated     → global.sandboxRuntimeClass
                                      └─ confidential → global.confidentialSandboxRuntimeClass
                                      │
                              output: Argo Rollout   ◄── ALWAYS, every path
                                .spec.template.spec.runtimeClassName
                                .spec.template.metadata.annotations
                                      │
                                      ▼
                     kube-apiserver: RuntimeClass admission controller
                       force-merges scheduling.nodeSelector + tolerations,
                       applies overhead.podFixed
                                      │
                  ┌───────────────────┼───────────────────┐
                  ▼                   ▼                   ▼
          isolated-sandbox    confidential-sandbox      kata-clh
          Nitro microVM       Nitro microVM,            Kata microVM,
          node trusted        node untrusted            nested-virt pool
```

Two things follow from the admission controller doing the placement, and both are why the runtime
swap was cheap:

- The pod spec names **no node label and no toleration**. The Nitro classes carry
  `eks.amazonaws.com/nitro-sandbox: "true"` as their nodeSelector and no taint; the Kata classes
  carry `katacontainers.io/kata-runtime` and a matching toleration. The component says neither.
- `overhead.podFixed` is applied for scheduling *and* kubelet accounting, so a Nitro pod is charged
  256Mi and one `eks.amazonaws.com/nitro-sandbox` slot without the Application mentioning either.

### 3.1 Pod annotations

Nitro adds a second channel the Kata design had no use for: `sandbox.amazonaws.com/*` pod
annotations. Three are plumbed through.

| Annotation | From | Why it is exposed |
|---|---|---|
| `policy` | `cedarPolicy` | The Cedar policy, enforced from inside the guest and measured into PCR17 |
| `conntrack-limit` | `sandboxConntrackLimit` | Default 4096 outbound connections per IP family, per pod. An agent holding streams to a model gateway and N MCP servers is an unusually heavy consumer |
| `dns-packets-per-second` | `sandboxDnsQueriesPerSecond` | Default 100 q/s, per pod. An agent that resolves its endpoints per request can exceed it under load |

The throttle defaults are generous; they are exposed because they are **per pod and invisible**.
Exceeding them shows up as dropped connections and failed resolution *inside the guest*, with
nothing reported at the Kubernetes layer.

**They go on `spec.template.metadata`, never on the Rollout.** Kubernetes does not copy a
controller's annotations into the pods it creates, so a `sandbox.amazonaws.com/` key on the
controller is accepted and then silently ignored. The EKS user guide calls this out for Deployments;
a Rollout has the same shape.

`network-isolated` is deliberately **not** on the agent component — see §6.

## 4. The Cedar policy, and why confidential fails closed without one

A confidential pod with no policy annotation is the subtlest failure in this design, so it is
rejected at admission.

**The runtime's behaviour here CHANGED during the preview, and both versions are in the wild.**
Which one you get depends on the AMI, so neither is safe to assume.

*Originally* — measured 2026-08-20 on `nitro-prod` (m7i.xlarge, private preview) — an unpolicied
confidential pod **ran**, and `kubectl exec … -- id` returned `uid=0(root)`. Confidential mode
behaved like isolated: the memory boundary held, but `exec`, `stdin` and process listing were all
allowed from the parent instance, so the node operator the mode exists to exclude could read the
workload by exec'ing into it. Exec denial was **policy-driven, not automatic**. That is what this
section used to describe, and it was accurate for that AMI.

*Now* — measured 2026-11-11 on `hub` (EKS Auto Mode, k8s v1.37.1, c8a.large) — the same pod shape
never starts:

```
FailedCreatePodSandBox  ...  CreateSandbox: resolve sandbox policy:
  confidential-sandbox requires the sandbox.amazonaws.com/policy annotation;
  a policy is required in confidential mode (default-deny is enforced)
```

It loops in `ContainerCreating` for as long as it is left there, retrying every ~15s, with the
reason only in kubelet events.

**Why the guard matters either way, and why it is not redundant now.** The two behaviours fail in
opposite directions, and `cedarPolicy` is required for a different reason under each:

| AMI behaviour | Without a policy | What the guard buys |
|---|---|---|
| Aug 2026 preview | pod **runs**, `exec` allowed — confidential in name only | prevents a silently-unprotected pod. This is the dangerous case |
| Nov 2026 (default-deny) | pod **never starts**, cause buried in kubelet events | turns an indefinite `ContainerCreating` loop into an immediate admission error naming the generator |

So: do **not** read "the runtime fails closed now" as "the policy is optional". On an older AMI it
is the only thing standing between you and an unprotected pod, and nothing in a manifest tells you
which AMI your nodes are running. Require it unconditionally, which is what the guard does.

PCR17 also measures the policy, so with no policy there is nothing for a KMS key policy to pin:
attestation becomes a number nobody checks. That is true on every AMI.

**Where exec-denial actually comes from (measured 2026-11-11).** `--profile confidential` denies
`Agent::Action::"Exec"` in the policy it generates, with no extra flags passed. A pod running such a
policy refuses `kubectl exec` with `policy denied Agent::Action::"Exec" on container "c"`. So
`--deny-exec`/`--deny-logs`, which `gen-cedar-policy.sh` adds by default, are belt-and-braces rather
than the thing creating the denial. They stay because the profile's contents are not a documented
contract and an explicit deny costs nothing.

This also closes the loop on the August observation: exec was allowed there because there was **no
policy at all**, not because confidential mode permits exec. Given a policy, it does not.

That is the same shape as an unconfigured RuntimeClass — isolation requested, isolation not
delivered, nothing reporting a problem — which `DESIGN.md` already fails closed on. So:

```cue
if parameter.sandbox && parameter.sandboxMode == "confidential" && parameter.cedarPolicy == "" {
    _cedarPolicyRequired: "set" & "sandboxMode: confidential requires cedarPolicy: ..."
}
```

A conflicting unification rather than a bare `_|_`: KubeVela prints both sides of a failed
unification, so the error carries the explanation. A `_|_` reports only `explicit error in source`
and leaves the developer guessing.

The policy has to be generated from the **final** pod spec, which is a real operational burden and
is stated as such in both confidential examples. `scripts/nitro/gen-cedar-policy.sh` exists because
`eks-nitro-sandbox-genpolicy` takes a pod manifest and an OAP developer has an OAM Application: the
script renders the Application the way the platform does (Helm first, so the globals are substituted
into the CUE), extracts the pod template from the Rollout or SandboxTemplate, and prints the base64
policy plus the KMS condition block. `--from rollout/<name>` reads the live object instead, which is
the post-webhook spec and therefore the one that matters once anything mutates pods.

## 5. Per-user isolation (#99)

### 5.1 The pattern

The agent definition is a template; a session is a claim against it; a warm pool absorbs cold start;
a TTL reclaims the instance. That is how AgentCore Runtime, the Kubernetes SIG `agent-sandbox`
project, and hosted sandboxes like E2B and Modal all model it, and the platform already runs the
primitives for the Dark Factory coder.

```
  type: agent                            type: sandbox
  isolation: session                     ("declared once")
  sandboxTemplate: analyst-sbx
        │                                      │
        │  Rollout (FRONT DOOR)                ├─ SandboxTemplate   the pod template
        │   resolves the session key            ├─ SandboxWarmPool   pre-booted instances
        │   claims / routes / releases          └─ ConfigMap         the "sandbox card"
        │                                      │
        └──── SandboxClaim per (agent, user, session) ────► one instance, one session
                                                            destroyed on idle or TTL
```

### 5.2 Why a new component type, when `DESIGN.md` §6 option D rejected one

Option D was *a second way to declare an agent* — an `agent-sandbox` component duplicating
blue-green, replicas, routing, memory and evaluation, which would drift from `agent` immediately.
That rejection stands, and this design honours its principle: **changing an agent's isolation never
means changing its component type.** It is still one boolean and one word on `type: agent`.

`type: sandbox` declares something an agent is not. It has no Service identity of its own, no
blue-green, no gateway route, no conversation and no Agent Card; one instance serves exactly one
session and is then destroyed. It shares no fields with `agent` beyond the isolation primitives both
sit on. Different lifecycle, different object.

### 5.3 The warm pool is the workload

`output` is the `SandboxWarmPool`, not the `SandboxTemplate`, with health gated on
`readyReplicas >= spec.replicas`.

A template always applies cleanly — it is a template — so an Application fronted by it would report
**Healthy on a cluster with no sandbox-capable node**, while every instance it ever handed out sat
`Pending`. The pool is the only object here with a status worth believing. `warmPool: 0` is still
Healthy (`0 >= 0`): a pool asked for nothing and holding nothing is correct, and means on-demand
claims with full cold start.

The pool is rendered even at `replicas: 0` because `SandboxClaim`'s only required field is
`warmPoolRef` — with no pool there is nothing for a session to claim.

### 5.4 What the agent component renders for the per-session tiers

- **Four env vars** carrying the declaration: `AGENT_ISOLATION`, `SANDBOX_WARM_POOL`,
  `SANDBOX_NAMESPACE`, `AGENT_SESSION_KEY`.
- **A Role and RoleBinding** on the agent's own ServiceAccount: `create`, `get`, `list`, `watch`,
  `delete` on `sandboxclaims`, and read-only on `sandboxes`.

A Role rather than a ClusterRole, so a claim outside the agent's namespace is denied — that is what
stops one tenant's agent claiming instances out of another namespace's pool. No write verb on
`sandboxes`: an agent that could delete a Sandbox could destroy another session's instance, and
`update` on a claim would let it extend its own TTL past the declared lifecycle. `delete` on claims
**is** granted, because releasing on session end rather than waiting out the idle TTL is the
difference between a warm pool that recycles and one that stays exhausted.

### 5.5 Session key

`sessionKey: subject+context | subject | context` answers #99's open question 1 as a **declaration**
rather than a platform convention, because the right answer depends on what the agent's tenants are
and only its developer knows that. The default, `subject+context`, is the gateway subject plus the
A2A `contextId`, falling back to the subject alone when a client sends no context — so an
unconfigured client gets its own sandbox per user rather than sharing with another user.

### 5.6 Two fail-closed gates

`isolation: session` is rejected at admission unless **both** hold:

1. `global.sessionIsolationEnabled == "true"` (cluster-secret annotation `session_isolation`), and
2. `sandboxTemplate` names a sandbox.

Deliberately separate from `enable_agent_sandbox`: having the CRDs is necessary but not sufficient,
because the agent **image** has to claim per session. Without the gate, an agent asking for
`isolation: session` on an unprepared cluster would render, deploy, serve traffic and silently share
one process between every user — the exact failure #99 exists to remove, and invisible precisely
because the agent still works.

### 5.7 What is NOT implemented

**The session router inside the agent.** This design delivers the declaration, the claim RBAC, the
env and the sandbox to claim from. What is not written is the agent-side half: resolving the session
key from the JWT `sub` and the A2A `contextId`, creating the `SandboxClaim`, pinning subsequent
requests to the bound instance, and releasing on session end. That is
`applications/strands-agent-base` work, tracked as the follow-up to this change.

Until it exists, `sessionIsolationEnabled` stays `"false"` and `isolation: session` is rejected
rather than half-delivered. That is why the gate is a separate switch from the `agent_sandbox`
addon: having the CRDs says nothing about whether anything can consume them.

**The on-EKS code-execution backend**, which §2.2 identifies as the right home for per-session tool
isolation, is also not written. The shape is small and needs no new component: a `sandbox.js` backend
beside `applications/code-interpreter-mcp`'s `src/agentcore.js`, selected by a config value, creating
a `SandboxClaim` keyed on the session instead of calling `StartCodeInterpreterSession`. `server.js`,
`tools.js` and `session.js` are unchanged, so the agent sees the identical tool surface either way —
this platform's own "managed or open source, per capability" split, applied to code execution. It is
independent of the router above and can land first.

**What does work today, with nothing else built, is the sandbox claimed directly** — see
`platform/oam/examples/example-sandbox-isolated.yaml`, which carries the `SandboxClaim` shape, and
the Dark Factory, which has run that pattern on this platform since V2 with Argo Workflows as the
claimer. An orchestrator, a batch runner, or a service of your own that fronts users and holds the
session map all work now. That is the adoption order this design recommends.

Also not implemented: `SandboxClaim` lifecycle TTLs are **published** on the sandbox card, not
enforced by this component, because a claim is created per session at runtime. A claimer that
ignores them leaks instances; declaring them in one place is what makes that a bug with an owner.

## 6. Decisions worth stating, with the reasoning

**`networkIsolated` is on `type: sandbox` and not on `type: agent`.** A network-isolated pod has no
DNS, no IMDS, no Pod Identity and no routable interface. An agent needs all of them: the model
gateway, every MCP server, the trace endpoint. There is no useful network-isolated agent, so the
field is not offered on the agent — rather than offered and then documented as a mistake. On a
sandbox running model-written code it is the right default to reach for, and it is *stronger* than an
egress NetworkPolicy because the device is **absent** rather than filtered: there is nothing for a
compromised workload to talk its way around.

**`confidential` + `networkIsolated` is rejected.** An OAP judgement, not an AWS restriction — both
are supported and the combination boots. It is rejected because confidential mode's value is
*attested secret release*, and a sandbox with no network has no route to KMS: the pod would run, the
memory boundary would hold, and every call that needed a secret would fail at the moment it was
needed. The two real shapes stay reachable: `isolated + networkIsolated` for code that must not
phone home, `confidential` for data that must survive an untrusted operator.

**`networkIsolated` + `service: true` is rejected.** A Service that selects a network-isolated pod
refuses every connection (the pod IP is still configured in the pod network namespace, so
connections are refused rather than dropped). Rendering it would publish an endpoint guaranteed to
fail. Such a sandbox is still fully usable — logs, exec, stdin and stats work over vsock.

**The `sandbox` ComponentDefinition autodetects its workload kind.** With an explicit
`workload.definition` naming `SandboxWarmPool`, applying the definition to a cluster without the
agent-sandbox operator is rejected by KubeVela's own mutating webhook (`no matches for kind
"SandboxWarmPool"`), which would leave the **entire** `oam-agent-components` Application unable to
sync — every component and trait in it, not just this one. `autodetects.core.oam.dev` defers the kind
to render time, where the error belongs: the definition installs harmlessly and an Application that
*uses* it fails with a message about the missing CRD. Found by `kubectl apply --dry-run=server`
against a hub that has the Nitro classes but not the operator.

**Both RuntimeClasses come from the cluster secret, not the chart.** `sandbox_runtime_class` and
`confidential_sandbox_runtime_class` annotations, the same mechanism as `aws_region`. The registry
previously pinned `kata-clh` fleet-wide, so one value decided the runtime for the hub and every
spoke. Per-cluster is what lets a Nitro hub and a Kata spoke disagree — including a spoke below
Kubernetes 1.36, which has no `isolated-sandbox` class at all and would otherwise leave every
sandboxed pod `Pending`.

**This repo does not render the Nitro RuntimeClasses.** It owns the Kata ones because `kata-deploy`
cannot express their tolerations. `isolated-sandbox` and `confidential-sandbox` belong to EKS. A
second owner of the same object names is exactly the failure
`agent-sandbox/templates/10-runtimeclasses.yaml` already records against `kata-deploy`: both apps
permanently OutOfSync.

## 6.1 Known footgun: two components, one name

Found by `scripts/test-isolation-matrix.sh` (row D1) rather than by review, and worth stating because
this change makes it easier to hit: pairing an agent with its own pool in one Application invites
naming them similarly.

**KubeVela resolves a component's type by NAME.** Two components sharing a name are not rejected —
the second definition wins for both. An Application with an `agent` and a `sandbox` both called
`foo` renders:

```
SandboxWarmPool/foo     <- the agent's workload, replaced
ServiceAccount/foo      <- emitted twice
Service/foo-stable      <- still here, selecting a Rollout that does not exist
Service/foo-preview
ConfigMap/foo-card
HTTPRoute/foo
(no Rollout at all)
```

Nothing errors, at render or at apply. The agent is simply never deployed, while every object that
advertises it still exists — which is the worst available shape, because the gateway route resolves
and the Application reports healthy.

Neither component can guard against it: KubeVela renders components independently, so CUE in one
cannot see the other. Three things mitigate it instead: the matrix row, which counts emitted
`(kind, name)` pairs across the whole Application; comments in `sandbox.cue` and
`example-agent-session-isolation.yaml`; and the examples themselves naming the pair distinctly
(`data-analyst` / `analyst-session`).

Not a collision, and asserted as such so a future rename does not quietly make it one: a sandbox
`X` and a sandbox `X-template`. The first emits `SandboxTemplate/X-template`, the second
`SandboxWarmPool/X-template` — same name, different kinds, which Kubernetes allows (matrix row D2).

Across Applications there is no such ambiguity and no silent merge, but there is still a conflict:
two Applications in one namespace each declaring a pool of the same name fight over all four of its
objects, and whichever syncs last owns resources the other believes it manages (matrix row D3).
That is ordinary Kubernetes name collision rather than anything new here, and the matrix detects it
by intersecting emitted objects across renders.

## 7. Kata is not impacted

Verified as a property of the render, not an intention:

| Check | Result |
|---|---|
| `--set global.sandboxRuntimeClass=kata-clh` on a `sandbox: true` agent | `runtimeClassName: kata-clh`, nothing else differs |
| `runtime: kata` on the `agent-sandbox` chart | SandboxTemplate byte-identical to `origin/main`, with its nodeSelector and toleration. The chart's DEFAULT is now `nitro` (validated on hardware 2026-11-11), so a Kata cluster pins `sandbox_runtime: kata` — see §7.2 |
| `sandboxMode: confidential` on a cluster with no confidential class | render **rejected**, with a message saying Kata has no equivalent — rather than a `kata-clh` pod that cannot attest |
| Every Kata value, class and comment in `agent-sandbox/values.yaml` | unchanged |
| `example-agent-simple`, `-with-mcp`, `-minimal`, `-milvus-memory`, `-with-browser`, `-with-code-interpreter`, `-centralized-observability`, `-mcp-local` rendered against `origin/main` | **byte-identical** (8 of 12) |
| `example-agent-evaluation`, `-imageless`, `-multidim`, `-smoketest` | **differ, and the difference is NOT from this work** — see below |

A Kata cluster is one annotation: `sandbox_runtime_class: kata-clh`, and leave
`confidential_sandbox_runtime_class` empty so confidential fails closed there.


### 7.2 The default moved to Nitro, and what a Kata cluster must do

Review finding 1 objected to a Nitro default on two grounds. Both are now
settled, in opposite ways:

- **"No per-cluster knob"** — the real defect, and fixed: the registry entry
  reads a `sandbox_runtime` annotation off the cluster secret, so a Kata cluster
  pins Kata without overriding the chart. That knob did not exist when the
  finding was written, which is what made a default flip dangerous.
- **"Until Nitro is validated live"** — done, 2026-11-11, on a k8s v1.37.1 EKS
  Auto Mode cluster. A `c8a.large` sandbox node was provisioned on demand in
  ~2 min; the pod reported kernel `6.18.54` against the node's `6.18.56`, DMI
  `c8a.enclave`, virtio disks; `verify-isolation.sh` gave 7 PASS / 0 FAIL. A
  confidential pod with a real Cedar policy ran too, with `exec` denied.

So the default is `nitro`, for the reason it was proposed: it needs **no node
layer of ours**. Both Kata prerequisites (`kata_nodepool`, `agent_sandbox_kata`)
ship `false` in every overlay here, so a cluster that enables this chart and
nothing else gets a pool that cannot place a single pod on the Kata default —
whereas Auto Mode provisions a sandbox node for the Nitro default on demand.

**A cluster whose nodes are Kata must set `sandbox_runtime: kata`** before this
reaches it. Otherwise its SandboxTemplate is recreated on a Nitro class, the Kata
nodeSelector and toleration vanish with it, and every warm pod goes Pending. No
cluster in this repo is affected — `dark_factory` is `false` everywhere, so
nothing claims from this pool — but a consumer's cluster can be, which is why the
migration note sits in `values.yaml` next to the value rather than only here.

**Dark Factory stays on Kata by pinning, not by default.** Its coder flow claims
from this pool, and a cluster running it on nested-virt nodes sets the annotation.
That is the right shape: the consumer that needs Kata says so, instead of every
other cluster inheriting a default it cannot schedule.

**The OAM layer is a separate knob and did not move.** `sandbox: true` and
`type: sandbox` on an Application are governed by `oam-agent-components`'
`global.sandboxRuntimeClass`, which has always defaulted to the Nitro classes.

### 7.1 The one difference, and why it is not a regression from this work

Regenerating `templates/agent.yaml` from `agent.cue` changes the four
`example-agent-evaluation*` renders:

```diff
-  backend: centralized
-  annotations: {}
+  annotations:
+    evaluation.oap.io/observability-mode: centralized
```

**This is not caused by anything on this branch.** `main`'s committed
`templates/agent.yaml` is STALE relative to `main`'s own `agent.cue`: the CUE
already replaced the `backend` field with the annotation, and the generated YAML
was never regenerated to match. Any change that regenerates that file picks the
difference up, and this branch regenerates it because it has to.

How to see that for yourself — and note that the FIRST of these is the
comparison that misleads:

```console
# From CUE on both sides: all 12 non-sandbox examples identical, including the
# evaluation four. This hides the drift, because it renders main's CUE rather
# than main's committed YAML.
# From the COMMITTED chart on both sides -- what actually ships -- 8 identical,
# these 4 different.
$ helm template r gitops/addons/charts/oam-agent-components   # per side
$ vela dry-run --offline -d <defs> -f platform/oam/examples/example-agent-evaluation.yaml
```

The change itself is correct and should land: the annotation is what
`agent.cue` has specified for some time, and `backend` is a field the evaluation
addon no longer reads. It is carried here as **its own commit**
(`regenerate: pick up pre-existing agent.cue drift`) so it can be reviewed,
reverted or landed separately from the isolation work, and so this document's
zero-regression claim stays honest about which files it covers.


## 8. Alternatives considered

| Option | Verdict |
|---|---|
| **A. Swap the default class, add `sandboxMode`, keep one component for agents** | **Chosen.** The runtime swap is one value because `DESIGN.md`'s mechanism already put placement in the admission controller. `sandboxMode` is the minimum new developer surface that expresses a trust boundary without naming a technology |
| **B. Expose `runtimeClassName` to developers** | Rejected. Ambient environment config in portable OAM (steering §1), and it would let an Application claim confidential isolation on a cluster that cannot provide it |
| **C. One field conflating runtime and trust model** (`sandbox: nitro \| nitro-confidential \| kata`) | Rejected. Collapses a portable property (trust model) with a non-portable one (technology), so every confidential Application becomes cluster-specific |
| **D. Keep Kata as the default, make Nitro opt-in** | Rejected. The default that cannot schedule a pod on a stock cluster is the wrong default. Nitro needs no node layer on Auto Mode; Kata needs two addons and a node pool, all off today |
| **E. Render the Nitro RuntimeClasses from this repo** | Rejected. EKS owns them; a second owner means both apps permanently OutOfSync |
| **F. `isolation: session` by rendering SandboxTemplate *instead of* a Rollout from `agent`** | Rejected. `attributes.workload.definition` is static per ComponentDefinition, so a component cannot change its workload kind on a parameter — and it would forfeit blue-green, replicas and the health gate the Rollout provides. Splitting the front door (`agent`) from the instance (`sandbox`) keeps all of them |
| **G. Enforce session TTLs in the component** | Not possible. The TTL lives on the `SandboxClaim`, created per session at runtime. Published on the sandbox card instead |
| **H. Emit an IAM policy for `kms:Decrypt` from the sandbox component** | Deferred. A KMS **key policy** can grant the Pod Identity role directly, which the attestation example does, and it keeps the PCR conditions and the grant in one document. Adding a fifth IAM-policy-emitting component also means extending `scripts/check-iam-names.sh`; worth doing when a second consumer needs it |

## 9. Validation

### 9.1 Done — rendering and admission

Offline, via `helm template` of `oam-agent-components` followed by `vela dry-run --offline` against
the Helm-substituted ComponentDefinitions. Testing with the chart default proves nothing, so every
value check uses a non-default.

| Check | Result |
|---|---|
| Default `sandbox: true` | `runtimeClassName: isolated-sandbox` |
| `sandboxMode: confidential` + policy | `runtimeClassName: confidential-sandbox`, `sandbox.amazonaws.com/policy` on the pod template |
| `--set global.sandboxRuntimeClass=kata-clh` | `kata-clh` — the Kata path is intact |
| `--set global.confidentialSandboxRuntimeClass=` + confidential | render **rejected**, message names the value and the annotation |
| `sandboxMode: confidential` with no `cedarPolicy` | render **rejected**, message names the script that generates one |
| `sandboxConntrackLimit` / `sandboxDnsQueriesPerSecond` | both annotations rendered, as strings |
| `isolation: session` on a default cluster | render **rejected**, message names the addon and the value |
| `isolation: session` + `sessionIsolationEnabled=true` | four env vars, claim Role and RoleBinding, warm pool of 3 on `isolated-sandbox` |
| `networkIsolated: true` with default `service: true` | render **rejected** |
| `type: sandbox` default | SandboxWarmPool, SandboxTemplate, ServiceAccount, sandbox card; `runtimeClassName: isolated-sandbox`; no nodeSelector |
| **Zero regression:** the 8 non-sandbox examples other than the evaluation four, vs `origin/main` | **byte-identical** |
| **Zero regression:** the 4 `example-agent-evaluation*` examples | **differ** — pre-existing CUE-vs-YAML drift on `main`, picked up by regenerating. See §7.1 |
| `kubectl apply --dry-run=server` on both regenerated definitions (hub, k8s 1.37) | both accepted |
| `kubectl apply --dry-run=server` on `pod-hello-sandbox.yaml`, `pod-attested.yaml` | both accepted, including `supplementalGroupsPolicy: Strict` |
| `agent-sandbox` chart, `runtime: nitro` | `isolated-sandbox`, no Kata nodeSelector or toleration |
| `agent-sandbox` chart, `runtime: kata` | `kata-clh` with its nodeSelector and toleration, unchanged |
| `agent-sandbox` chart, `nitro.mode=confidential` with no policy | Helm render **fails** with the reason |
| `scripts/nitro/download-runtime-binaries.sh` | signature `Verified OK`; all 8 files `OK` against the signed manifest |
| `scripts/nitro/gen-cedar-policy.sh` (stub generator) | resolves `SandboxTemplate/analyst-session-template` and `Rollout/regulated-assistant` from their Applications |
| `scripts/nitro/verify-isolation.sh` on a non-sandboxed pod | correctly reports no `runtimeClassName`, exits 1 |
| **`scripts/test-isolation-matrix.sh`** — 21 examples, 10 composition rows, 11 guard rows, 5 collision rows | **0 failures**, 4 skips (examples needing appmod-blueprints traits) |

### 9.2 Outstanding — runtime, on a sandbox-capable node

Nothing below can be checked by rendering; each needs a Nitro node.

1. **`pod-hello-sandbox.yaml` runs.** `.enclave` DMI product name, virtio block devices, and — the
   decisive one — a guest kernel differing from the node's. A container cannot have a different
   kernel from its host.
2. **A sandboxed agent serves traffic.** `/health` answering, reachable through the stable Service
   and the HTTPRoute, and in-guest DNS to Bifrost and each MCP server.
3. **Pod Identity across the boundary.** The least certain item. `aws-service-identity` depends on
   the node-local credentials endpoint being reachable from inside the guest. IMDS inside a sandbox
   serves only placement metadata, so anything expecting IAM credentials from IMDS will fail.
4. **Blue-green in the sandbox path.** Preview ReplicaSet of microVMs comes up, preview Service
   resolves it, promotion swaps.
5. **Confidential end to end.** A real `eks-nitro-sandbox-genpolicy` run (the generator is Linux
   ELF; it has only been exercised here through a stub), a KMS key with the PCR17 and
   PCR8:ServiceName conditions, an encrypted image, the pod reaching Running — **and the negative
   control**: stale PCR17 → `AccessDeniedException` on the pull. A gate nobody has seen reject
   anything is not known to be a gate.
6. **Warm pool fills.** `SandboxWarmPool` `readyReplicas` reaching `spec.replicas` on Nitro, and the
   Application reporting Healthy only then.
7. **Density under load.** The per-node ceilings (total sandbox vCPUs ≤ 5× the node's; instance
   count likewise, halved on CPU-overcommit types) are documented, not measured.

### 9.3 A live-cluster finding, unrelated to this branch

On the hub used for this work, the installed `agent` ComponentDefinition contains an
**unsubstituted Helm placeholder**:

```console
$ kubectl get componentdefinition agent -n vela-system \
    -o jsonpath='{.spec.schematic.cue.template}' | grep _sandboxRuntimeClass
_sandboxRuntimeClass: "{{ .Values.global.sandboxRuntimeClass }}"
```

It carries a `kubectl.kubernetes.io/last-applied-configuration` annotation, so it was applied with
`kubectl` directly rather than rendered by Helm through Argo — and the `oam-agent-components-hub`
Application is `OutOfSync` as a result. Any `sandbox: true` agent deployed against that definition
gets a `runtimeClassName` that is not a valid class name and is rejected outright.

Sync the Application before testing. `scripts/nitro/verify-isolation.sh` detects this specific case
and names it, because nothing else in the stack does.

### 9.4 Open, and deliberately not claimed

Recorded here rather than left implicit, because each one is something a reader
could reasonably assume was verified.

- **`SandboxTemplate.networkPolicyManagement` is left at the CRD default
  (`Managed`).** With `service: true` the operator creates the NetworkPolicy, and
  nothing here checks that the policy it creates actually admits the Service's
  traffic. If a claimed sandbox is reachable on its Service but connections are
  refused, this is the first thing to look at: compare the operator-created
  policy against the Service's selector and port. Needs a cluster with the
  sandbox operator to settle.
- **`sessionIsolationEnabled` is a declarative switch.** It asserts that the
  cluster has the `agent_sandbox` addon AND an agent image that claims a sandbox
  per session. The first half is a real prerequisite the guard names; the second
  cannot be checked from a render — nothing verifies the image actually calls
  `SandboxClaim`. An agent on an image that does not will render, deploy, serve
  traffic and share one process, which is the failure the switch exists to
  prevent. A readiness probe or a startup assertion in the image is the real
  answer; the switch is the honest interim.
- **No Nitro node was available for this work.** Guest kernel and DMI,
  Pod Identity from inside the microVM, blue-green through a sandboxed Rollout,
  warm-pool fill behaviour, density, and end-to-end attestation with an encrypted
  image are all unverified on hardware. Everything claimed above is a render-time
  or chart-time property, measured offline.


## 10. Files

| # | File | Change |
|---|---|---|
| 1 | `platform/oam/definitions/components/agent.cue` | `sandboxMode`, `cedarPolicy`, throttle knobs, `isolation` / `sandboxTemplate` / `sessionKey`, five fail-closed guards, claim RBAC, card fields |
| 2 | `platform/oam/definitions/components/sandbox.cue` | **New.** `type: sandbox` |
| 3 | `gitops/addons/charts/oam-agent-components/values.yaml` | `sandboxRuntimeClass` default → `isolated-sandbox`; `confidentialSandboxRuntimeClass`; `sessionIsolationEnabled` |
| 4 | `gitops/addons/registry/agentcore.yaml` | all three sourced from cluster-secret annotations |
| 5 | `gitops/addons/charts/oam-agent-components/templates/{agent,sandbox}.yaml` | regenerated by `generate.sh` |
| 6 | `gitops/addons/charts/agent-sandbox/values.yaml`, `templates/20-sandboxtemplate.yaml` | `runtime: nitro \| kata` for the warm pool; `nitro.*` |
| 7 | `scripts/nitro/*` | download + verify binaries, generate a policy, verify isolation |
| 8 | `examples/nitro-sandbox/*` | the attestation walkthrough |
| 9 | `platform/oam/examples/example-{sandbox-isolated,sandbox-confidential,agent-sandbox-confidential,agent-session-isolation}.yaml` | new examples |
| 10 | `platform/oam/examples/example-agent-sandbox.yaml` | retargeted at the Nitro default |

## 11. Reading

- [`DESIGN.md`](DESIGN.md) — the Kata design this extends
- [Sandboxing on Amazon EKS with isolated and confidential pods](https://docs.aws.amazon.com/eks/latest/userguide/sandboxing.html)
- [Considerations](https://docs.aws.amazon.com/eks/latest/userguide/sandboxing-limitations.html) — read before sizing anything
- [Security model and parent instance trust assumptions](https://docs.aws.amazon.com/eks/latest/userguide/sandboxing-security-model.html)
- [AWS Nitro Sandboxes: hardware-enforced workload isolation on Amazon EKS](https://aws.amazon.com/blogs/containers/aws-nitro-sandboxes-hardware-enforced-workload-isolation-on-amazon-eks/)
- [`examples/nitro-sandbox/README.md`](../../examples/nitro-sandbox/README.md) — attestation, end to end
- Issue [#99](https://github.com/awslabs/open-agentic-platform/issues/99), and [#78](https://github.com/awslabs/open-agentic-platform/issues/78) / [#66](https://github.com/awslabs/open-agentic-platform/issues/66) for what came before
