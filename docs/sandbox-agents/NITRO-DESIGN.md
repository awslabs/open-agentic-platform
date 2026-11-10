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
    isolation: session            # NEW: scope.      shared (default) | tools | session
    sandboxTemplate: analyst-sbx  # NEW: which sandbox the per-session tiers claim from
```

| Field | What it answers | Who owns it |
|---|---|---|
| `sandbox` | Is there a hardware boundary at all? | developer |
| `sandboxMode` | **Who is inside** the boundary — is the node trusted? | developer |
| `isolation` | **How many** boundaries — one per agent, or one per session? | developer |
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

| | `shared` (default) | `tools` | `session` |
|---|---|---|---|
| Agent process | one, for everyone | one, for everyone | one front door |
| Per-session sandbox | none | for code execution, filesystem, browser | a dedicated instance |
| Cost | lowest | low — a sandbox exists only while a tool runs | highest |
| Good for | Q&A, retrieval, read-only tools | anything running model-generated code | regulated or multi-tenant work, long-lived per-user state |

`tools` is the recommended first step, and not as a compromise: a shared agent loop with
per-session code execution puts the boundary exactly where the prompt-injection blast radius is,
for a fraction of a VM per user.

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

The sandbox falls back to a **permissive built-in default**: every container admitted, and `exec`,
`stdin` and process listing all allowed from the parent instance. The memory boundary still holds,
so the pod runs and looks right — while the node operator the mode exists to defend against reads
the workload's data by exec'ing into it. And PCR17 measures the policy, so with no policy there is
nothing for a KMS key policy to pin: attestation becomes a number nobody checks.

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

`isolation: tools` and `isolation: session` are rejected at admission unless **both** hold:

1. `global.sessionIsolationEnabled == "true"` (cluster-secret annotation `session_isolation`), and
2. `sandboxTemplate` names a sandbox.

Deliberately separate from `enable_agent_sandbox`: having the CRDs is necessary but not sufficient,
because the agent **image** has to claim per session. Without the gate, an agent asking for
`isolation: session` on an unprepared cluster would render, deploy, serve traffic and silently share
one process between every user — the exact failure #99 exists to remove, and invisible precisely
because the agent still works.

### 5.7 What is NOT implemented

**The claim-and-route loop inside the agent.** This design delivers the declaration, the RBAC, the
env and the sandbox to claim from. Resolving the session key from the JWT `sub` and the A2A
`contextId`, creating the `SandboxClaim`, pinning subsequent requests to the bound instance and
releasing on session end is agent-image work (`applications/strands-agent-base`), tracked as the
follow-up to this change. Until an image does it, `sessionIsolationEnabled` stays `"false"` and the
tiers are rejected rather than half-delivered. That is why the gate is a separate switch from the
addon.

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

## 7. Kata is not impacted

Verified as a property of the render, not an intention:

| Check | Result |
|---|---|
| `--set global.sandboxRuntimeClass=kata-clh` on a `sandbox: true` agent | `runtimeClassName: kata-clh`, nothing else differs |
| `runtime: kata` on the `agent-sandbox` chart | SandboxTemplate byte-identical to before this branch, with its nodeSelector and toleration |
| `sandboxMode: confidential` on a cluster with no confidential class | render **rejected**, with a message saying Kata has no equivalent — rather than a `kata-clh` pod that cannot attest |
| Every Kata value, class and comment in `agent-sandbox/values.yaml` | unchanged |
| `example-agent-simple`, `-with-mcp`, `-evaluation` rendered against `origin/main` | **byte-identical** |

A Kata cluster is one annotation: `sandbox_runtime_class: kata-clh`, and leave
`confidential_sandbox_runtime_class` empty so confidential fails closed there.

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
| **Zero regression:** `example-agent-simple`, `-with-mcp`, `-evaluation` vs `origin/main` | **byte-identical** |
| `kubectl apply --dry-run=server` on both regenerated definitions (hub, k8s 1.37) | both accepted |
| `kubectl apply --dry-run=server` on `pod-hello-sandbox.yaml`, `pod-attested.yaml` | both accepted, including `supplementalGroupsPolicy: Strict` |
| `agent-sandbox` chart, `runtime: nitro` | `isolated-sandbox`, no Kata nodeSelector or toleration |
| `agent-sandbox` chart, `runtime: kata` | `kata-clh` with its nodeSelector and toleration, unchanged |
| `agent-sandbox` chart, `nitro.mode=confidential` with no policy | Helm render **fails** with the reason |
| `scripts/nitro/download-runtime-binaries.sh` | signature `Verified OK`; all 8 files `OK` against the signed manifest |
| `scripts/nitro/gen-cedar-policy.sh` (stub generator) | resolves `SandboxTemplate/analyst-session-template` and `Rollout/regulated-assistant` from their Applications |
| `scripts/nitro/verify-isolation.sh` on a non-sandboxed pod | correctly reports no `runtimeClassName`, exits 1 |

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
