# AWS Nitro Sandboxes: default isolation runtime, and confidential mode for agents

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
```

| Field | What it answers | Who owns it |
|---|---|---|
| `sandbox` | Is there a hardware boundary at all? | developer |
| `sandboxMode` | **Who is inside** the boundary — is the node trusted? | developer |
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

## 5. `type: sandbox` — a claimable warm pool

A component type for a pool of hardware-isolated execution environments: a
`SandboxWarmPool` (the workload) over a `SandboxTemplate` (the pod template its
instances are cloned from), plus a ServiceAccount and a card ConfigMap reporting
what the cluster actually gave you.

A consumer creates a `SandboxClaim` naming the pool; the operator binds a
pre-warmed instance and refills the buffer behind it. Verified on a live Nitro
cluster: a claim bound in under a minute to a sandbox with its own guest kernel,
and the pool refilled while it was held.

**The pool is the workload, deliberately.** The warm pool is what KubeVela
tracks, not the template, so `kubectl get application` reflects whether the pool
is actually at its desired replica count — a template is inert and would always
look healthy.

**What claims from it is out of scope here.** A consumer creates the
`SandboxClaim` — an orchestrator, a workflow step, or a tool server. The open
question is the **in-sandbox execution contract**: what runs inside, and how a
caller talks to it.


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
the per-session example that has since been removed; and the examples themselves naming the pair distinctly
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


## 8. Alternatives considered

| Option | Verdict |
|---|---|
| **A. Swap the default class, add `sandboxMode`, keep one component for agents** | **Chosen.** The runtime swap is one value because `DESIGN.md`'s mechanism already put placement in the admission controller. `sandboxMode` is the minimum new developer surface that expresses a trust boundary without naming a technology |
| **B. Expose `runtimeClassName` to developers** | Rejected. Ambient environment config in portable OAM (steering §1), and it would let an Application claim confidential isolation on a cluster that cannot provide it |
| **C. One field conflating runtime and trust model** (`sandbox: nitro \| nitro-confidential \| kata`) | Rejected. Collapses a portable property (trust model) with a non-portable one (technology), so every confidential Application becomes cluster-specific |
| **D. Keep Kata as the default, make Nitro opt-in** | Rejected. The default that cannot schedule a pod on a stock cluster is the wrong default. Nitro needs no node layer on Auto Mode; Kata needs two addons and a node pool, all off today |
| **E. Render the Nitro RuntimeClasses from this repo** | Rejected. EKS owns them; a second owner means both apps permanently OutOfSync |
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
  (`Managed`), and that now has an OBSERVED consequence.** With the addon running
  on the hub, the operator and the chart both try to own the same object and the
  operator reconciles in a permanent error loop:

  ```
  Failed to create NetworkPolicy ... SandboxTemplate=coder-sandbox
    networkpolicies.networking.k8s.io "coder-sandbox-network-policy" already exists
  ```

  Nothing breaks — the policy the chart created is in place — but the controller
  logs an error every reconcile, which buries real failures. Either set
  `networkPolicyManagement: Unmanaged` on the template the chart renders, or stop
  rendering the NetworkPolicy and let the operator own it. Also still unchecked:
  whether the operator-created policy admits the Service's traffic with
  `service: true`. If a claimed sandbox is reachable on its Service but connections are
  refused, this is the first thing to look at: compare the operator-created
  policy against the Service's selector and port. Needs a cluster with the
  sandbox operator to settle.

- **No Nitro node was available for this work.** Guest kernel and DMI,
  Pod Identity from inside the microVM, blue-green through a sandboxed Rollout,
  warm-pool fill behaviour, density, and end-to-end attestation with an encrypted
  image are all unverified on hardware. Everything claimed above is a render-time
  or chart-time property, measured offline.


## 10. Files

| # | File | Change |
|---|---|---|
| 1 | `platform/oam/definitions/components/agent.cue` | `sandboxMode`, `cedarPolicy`, throttle knobs, the Nitro-class guards and the shape checks |
| 2 | `platform/oam/definitions/components/sandbox.cue` | **New.** `type: sandbox` |
| 3 | `gitops/addons/charts/oam-agent-components/values.yaml` | `sandboxRuntimeClass` default → `isolated-sandbox`, plus `confidentialSandboxRuntimeClass` and `nitroSandboxRuntimeClasses` |
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
