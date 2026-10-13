# Sandbox-Isolated Agents — Kata MicroVM Isolation for the OAM `agent` Component

> **Status:** Implemented **and verified on a live EKS cluster**. One opt-in boolean runs an OAM
> `agent` inside a Kata microVM with a hardened `securityContext`. The workload stays an Argo
> Rollout in both paths, so blue-green, `replicas`, Services and gateway routing are unchanged.
>
> Both previously-outstanding steps are done. (1)
> `gitops/addons/charts/oam-agent-components/templates/agent.yaml` **is** regenerated on this branch
> via `platform/oam/generate.sh` (`vela` CLI 1.10.4 — it renders offline; the
> `Failed to load external packages for cuex default compiler` line is a non-fatal warning). Source
> and generated output are in sync: re-rendering the committed CUE reproduces the committed
> ComponentDefinition byte-for-byte. (2) The §8 validation plan has been run — see
> **§8.1 Validation results**.
>
> **Proof of isolation:** a sandboxed agent's pod reports guest kernel `6.18.35` against host
> `6.12.110-135.201.amzn2023.x86_64`. A pod cannot have a different kernel from its host, so this is
> a real microVM rather than a shared-kernel container.
>
> Two defects were found by that validation and fixed on this branch: a literal Helm action inside a
> CUE comment (which broke the render of the **entire** `oam-agent-components` chart once the
> template was regenerated), and a missing numeric `runAsUser` (which prevented `sandbox: true` from
> starting *any* image whose `USER` is a name — including this component's own default image). See
> §8.1.

## 1. Problem and contract

An agent runs today as an ordinary pod: a shared-kernel container on a general node pool. That is
the right default, and the wrong answer for an agent that executes model-generated code, handles
untrusted input, or serves more than one tenant — those want a hardware isolation boundary.

The platform already ships that boundary: Kata RuntimeClasses (`kata-clh`, `kata-qemu`, `kata-fc`)
on a dedicated Karpenter pool, used today by the Dark Factory coder. What is missing is a way to put
an **agent** on it without hand-writing a pod spec.

```yaml
- name: oap-assistant-a
  type: agent
  properties:
    sandbox: true          # ← the whole feature, from a developer's point of view
```

| `sandbox` | Behaviour |
|---|---|
| absent / `false` | Today's Argo Rollout, pod template unchanged, **byte-for-byte identical output** |
| `true` | The same Rollout, with two additions to `spec.template.spec`: `runtimeClassName` (platform-chosen, default `kata-clh`) and a hardened container + pod `securityContext` |

A developer cannot choose the VMM. That is a platform decision (§3).

## 2. Mechanism

Kubernetes' **built-in RuntimeClass admission controller** force-merges a class's
`scheduling.nodeSelector` and `tolerations` onto any pod selecting it, and applies its
`overhead.podFixed`. `agent-sandbox/templates/10-runtimeclasses.yaml` states it in its own header:
*"a workload only needs `runtimeClassName` to reach the right node pool."*

So isolation costs **one field** on the pod template the Rollout already has.

```
 OAM Application                 agent.cue
   type: agent        ────────►  output: Argo Rollout   ◄── ALWAYS, both paths
   properties:                     .spec.template.spec
     sandbox: true|false              sandbox=false ─► unchanged
                                      sandbox=true  ─► + runtimeClassName (platform)
                                                       + hardened securityContext
                                              │
                                              ▼
                              Argo Rollout → ReplicaSet → Pod
                              (blue-green, replicas, health gate: UNCHANGED)
                                              │
                        ┌─────────────────────┴─────────────────────┐
                        ▼ sandbox=false                             ▼ sandbox=true
            runc pod, general node pool          Kata microVM pod, kata node pool
                                                 (admission controller merges
                                                  nodeSelector + toleration + overhead)
```

**What this deliberately does not change:** no health-policy change (Rollout health already reports
an unschedulable pod as not-Ready); no `outputs` change (ServiceAccount, both Services, Agent Card
ConfigMap, HTTPRoute byte-identical — the Services select `app.kubernetes.io/name`, which the pod
carries either way); no new CRD, chart, controller or webhook; **no `Sandbox` CR, so the
agent-sandbox operator is not a prerequisite**; no default flips.

## 3. Who owns the VMM choice, and where it lives

**Personas.** A **platform engineer** owns the OAM definitions, charts and GitOps wiring and chooses
the Kata class. An **application developer** writes an OAM Application, never this repo, and sets
`sandbox: true` and nothing else. ("Operator" below always means a Kubernetes controller, never a
human role.)

The VMM is exactly the class of ambient environment value that `.kiro/steering/oam-authoring.md` §1
forbids in a developer's OAM, alongside region and account id. So there is **no
`sandboxRuntimeClass` developer parameter**.

**At runtime the class is stored in the `ComponentDefinition` object** —
`componentdefinition/agent` in `vela-system`, inside `spec.schematic.cue.template`, as an
already-substituted literal. `values.yaml` is only the *source* at build and sync time.

```console
$ kubectl get componentdefinition agent -n vela-system \
    -o jsonpath='{.spec.schematic.cue.template}' | grep _sandboxRuntimeClass
        _sandboxRuntimeClass: "kata-clh"
```

The Helm placeholder and the CUE around it resolve in **two different systems at two different
times**, which is why the placeholder is gone long before any developer deploys:

| Stage | Actor | What happens |
|---|---|---|
| 1. build | platform engineer | `agent.cue` carries `"{{ .Values.global.sandboxRuntimeClass }}"` as text; `generate.sh` passes it through verbatim into `templates/agent.yaml` |
| 2. sync | Argo CD | Helm renders the chart and substitutes the literal from `values.yaml` + `registry/agentcore.yaml`'s `valuesObject.global`. The ComponentDefinition lands in `vela-system` **with no Helm left in it** |
| 3. deploy | application developer | Applies an Application with `sandbox: true`. KubeVela evaluates the stored CUE — reading a constant |
| 4. admission | kube-apiserver | Pod carries `runtimeClassName`; the RuntimeClass admission controller merges scheduling and overhead |

This is not a new mechanism: `agentcore-memory.cue`, `agentcore-browser.cue`,
`agentcore-code-interpreter.cue` and the `aws-service-identity.cue` trait all carry
`{{ .Values.global.* }}` placeholders inside their CUE today. Verify without a cluster:

```console
$ helm template oam-test gitops/addons/charts/oam-agent-components \
    --set global.awsRegion=eu-west-1 | grep 'region: \*'
        	region: *"eu-west-1" | string      # ← substituted, inside the CUE string
```

## 4. The change

One logic file, two wiring values. Everything else is generated, an example, or docs.

| # | File | Change | Hand-edited |
|---|---|---|---|
| 1 | `platform/oam/definitions/components/agent.cue` | `sandbox` parameter, `_sandboxRuntimeClass` local + fail-closed guard, conditional `runtimeClassName`, conditional `securityContext` | **Yes** |
| 2 | `gitops/addons/charts/oam-agent-components/values.yaml` | `global.sandboxRuntimeClass: kata-clh` fallback | **Yes** |
| 3 | `gitops/addons/registry/agentcore.yaml` | `sandboxRuntimeClass` in `valuesObject.global`, beside `awsRegion` / `clusterName` / `awsAccountId` | **Yes** |
| 4 | `gitops/addons/charts/oam-agent-components/templates/agent.yaml` | Regenerated by `generate.sh` | No (generated) |
| 5 | `platform/oam/examples/example-agent-sandbox.yaml` | New example | Yes |
| 6 | `platform/oam/DESIGN.md`, `platform/oam/README.md` | Document the flag and the `runAsNonRoot` requirement | Yes |

### 4.1 `agent.cue`

**The developer parameter**, inside the existing `parameter` block:

```cue
// +usage=Run this agent inside a Kata microVM with a hardened securityContext.
// Default false = today's pod, byte-identical. The Kata VMM is a PLATFORM choice
// and is deliberately not expressible here.
sandbox: *false | bool
```

**The platform value**, at `template` scope and *outside* `parameter`. The leading underscore makes
it a CUE **local**, structurally unreachable from a developer's `properties` — that is what enforces
platform ownership, rather than a convention:

```cue
_sandboxRuntimeClass: "{{ .Values.global.sandboxRuntimeClass }}"

// FAIL CLOSED: an empty platform value with sandbox: true would silently schedule
// an ordinary runc pod — isolation requested, isolation not delivered, no error.
if parameter.sandbox && _sandboxRuntimeClass == "" {
    _|_ // "sandbox: true requires global.sandboxRuntimeClass on the
        //  oam-agent-components chart; no isolation runtime is configured."
}
```

Two notes. **A deliberate deviation:** steering §1 prescribes an *overridable* default
(`*"{{ ... }}" | string`); a local is non-overridable on purpose, because the isolation class is a
security boundary, not a portability knob. **Helm parses before CUE**, so the placeholder must be
valid Helm syntax with no nested double quotes — the fallback lives in `values.yaml`.

**Inject into the pod spec** at `output.spec.template.spec` (`agent.cue:72`), beside
`serviceAccountName`:

```cue
spec: {
    serviceAccountName: context.name          // UNCHANGED
    if parameter.sandbox {
        runtimeClassName: _sandboxRuntimeClass
    }
    containers: [{ /* existing container, unchanged except below */ }]
}
```

**Harden the container.** The microVM constrains what a compromised agent reaches on the *host*; it
does nothing about privileges *inside* the guest. `agent-sandbox-operator/values.yaml` is explicit
that coder sandboxes get isolation from *"the Kata micro-VM boundary **plus** the restricted
securityContext baked into the SandboxTemplate pod spec"* — this design supplies that second half
directly, since it uses no SandboxTemplate.

```cue
// container level, inside containers[0]
if parameter.sandbox {
    securityContext: {
        allowPrivilegeEscalation: false
        runAsNonRoot:             true
        capabilities: drop: ["ALL"]
        seccompProfile: type: "RuntimeDefault"
    }
}
// pod level, inside spec
if parameter.sandbox {
    securityContext: {runAsNonRoot: true, seccompProfile: type: "RuntimeDefault"}
}
```

`readOnlyRootFilesystem` is deliberately unset: agent images write to `/tmp` (SDK caches, OTel
buffers, bytecode), so enabling it needs an `emptyDir` and per-image verification — a follow-up, not
this change.

### 4.2 Chart and registry wiring

```yaml
# oam-agent-components/values.yaml — the fallback the placeholder resolves against
global:
  sandboxRuntimeClass: kata-clh

# gitops/addons/registry/agentcore.yaml — optional per-cluster override
valuesObject:
  global:
    sandboxRuntimeClass: kata-clh
```

### 4.3 Regeneration

`platform/oam/generate.sh` needs a **reachable KubeVela cluster**, not just the `vela` CLI, because
`vela def render` resolves cluster packages:
`KUBECONFIG=.platform/private/hub-kubeconfig ./generate.sh`. Confirm only `agent.yaml` changes. The
issue-#50 blocker no longer applies — `agent.cue` now carries the `opentelemetry-instrument` command
itself, so regeneration preserves tracing.

## 5. VMM options

All three classes are rendered unconditionally, so **a class existing proves nothing about whether a
pod can schedule on it** — a class with no node pool leaves pods `Pending` with no render error.

| | `kata-clh` | `kata-qemu` | `kata-fc` |
|---|---|---|---|
| **Default state** | **default, verified** (guest kernel 6.18.35 on spoke-dev) | declared, ready | declared but **inert** |
| **Node pool** | `kata-nested` (on by default) | **same pool as clh** | **separate** `kata-fc` pool (off) |
| **Label / taint** | `katacontainers.io/kata-runtime` / `kata` | same as clh | `…/kata-runtime-fc` / `kata-fc` |
| **Runtime app** | `kata-deploy` | same release | separate `kata-deploy-fc` |
| **Node storage** | overlayfs | overlayfs | **devmapper thin-pool** |
| **Kubelet overhead** | 130Mi / 250m | **320Mi** / 250m | 130Mi / 250m |
| **To make it the agent default** | nothing | `global.sandboxRuntimeClass: kata-qemu` — no node change | the three steps below **plus** `global.sandboxRuntimeClass: kata-fc` |

**Enabling `kata-fc` at the node level.** Steps 1 and 2 are both required; step 3 is warm-pool-only
and not needed for this feature.

| Step | File | Change | Why |
|---|---|---|---|
| 1 | `kata-nodepool/values.yaml` | `kataFc.enabled: true` | `nodepool.yaml` wraps the fc pool in `{{- if $cfg.enabled }}`; off means the NodePool is never rendered, so no node carries the fc label and every fc pod stays `Pending`. Also provisions the devmapper thin-pool via userData |
| 2 | `environments/<env>/enabled-addons.yaml` | `agent_sandbox_kata_fc: true` | Gates `kata-deploy-fc`, which installs the `kata-fc` containerd handler. Without it the node has the label but no runtime → hard creation error |
| 3 | `agent-sandbox/values.yaml` | `kata.vmm: fc` | Moves only the Dark Factory *warm pool* onto fc. Not needed for agents |

Both overlays ship every fc switch **off** today, commented *"UNVERIFIED on this platform — ported
from openclaw."* Nothing in the OAM layer enables any VMM: `agent.cue` emits a class name; whether
it schedules is the platform layer's doing.

**EKS Auto Mode is irrelevant** to this feature. `kata-nodepool` uses
`nodeClassRef.group: karpenter.k8s.aws`, commented *"Auto Mode (group `eks.amazonaws.com`) leaves
them alone"* — Kata runs on a self-managed pool that coexists with Auto Mode by group partition.

## 6. Alternatives considered

| Option | Verdict |
|---|---|
| **A. Inject into the existing Rollout pod template** | **Chosen.** Two conditional fields, one chart value; keeps blue-green, `replicas`, routing and health gating; no new CRD, controller or webhook |
| **B. Bind the class from the `env-config` `EnvironmentConfig`** | **Rejected — not implementable.** ADR-4 in `docs/architecture/agent-identity-and-token-exchange.md`: *"Only Compositions can consume it (not KubeVela, not raw MRs) — hence the `XPodIdentity` Composition indirection."* `runtimeClassName` must land in a pod template KubeVela renders, so the value has no path to the field. Note `aws-service-identity` splits on exactly this line: env-config for the IAM resources Crossplane creates, a Helm global for `AWS_REGION` because that is pod spec |
| **C. Emit a `Sandbox` CR as the workload** | Rejected — the v0.1/v0.2 approach. A Sandbox is one microVM pod, not a ReplicaSet, so it forfeits blue-green and `replicas`, needs a bespoke health policy, and adds an operator dependency for placement the admission controller already provides |
| **D. A separate `agent-sandbox` component type** | Rejected — duplicates all agent logic and drifts; changing isolation should not mean changing component type |
| **E. A mutating admission webhook** | Rejected for v1, **the right future path.** The only option giving real late binding and per-namespace variation (§7.2). Costs adopting Kyverno or operating a webhook for one field; the repo has no pod mutator today |
| **F. A KubeVela trait patching `runtimeClassName`** | Rejected — a trait is developer-opted and developer-visible, reintroducing the VMM as an app-level knob; also cannot carry the fail-closed guard cleanly |
| **G. `SandboxClaim` / the warm pool** | Rejected — tuned for ephemeral untrusted coder pods (idle-until-claim, scale-to-zero), not a long-lived service with a stable Service and gateway route |

## 7. Limitations and open questions

1. **`runAsNonRoot` is the one way `sandbox: true` can break a working image.** An image whose
   `USER` is root will fail to start. Intended — a root process inside a microVM is not the isolation
   the flag promises — but it is the single non-additive behaviour, so it belongs in
   `platform/oam/DESIGN.md`. *Open: keep hardening coupled to the flag (recommended), or split it
   into a second platform toggle?*
2. **No late binding — the class is cluster-wide and frozen at sync time.** The most significant
   limitation. Because the literal is baked in at stage 2, (a) changing a cluster's VMM does not
   migrate running agents until they next reconcile, and (b) there is **no per-app, per-namespace or
   per-test override**, so trying `kata-qemu` against a single agent means re-pointing the whole
   cluster. *Open: accept for v1 (recommended), add a non-prod-only override, or adopt the webhook
   (option E).*
3. **Failure is `Pending`, not an error.** A configured class with no matching node leaves pods
   `Pending`; the Rollout health gate reports not-Ready rather than fake-green. Fail-closed catches
   only the unconfigured-platform case — nothing in the OAM layer can see node labels at render time.
4. **Namespace Pod Security must permit the pod.** The agent runs in the application developer's
   namespace, not the hardened `agent-sandbox-system`. The injected context is
   `restricted`-compatible apart from `readOnlyRootFilesystem`, but **this was not verifiable from
   the repo** — confirm PSS labels on target namespaces before rollout.
5. **Capacity.** Each replica is a microVM and the class adds `overhead.podFixed` (320Mi on qemu) on
   top of pod requests. Expect slower start than runc; tight `initialDelaySeconds` or rollout
   progress deadlines may need loosening.
6. **Traits over the microVM boundary — verify on a real cluster.** `gateway-identity` mounts a
   projected SA token (pod-level, should be transparent). `aws-service-identity` uses **EKS Pod
   Identity**, which depends on the node-local credentials endpoint being reachable from inside the
   guest and on its `wait-for-aws-identity` init container succeeding there — the least certain item
   in this design.

**Resolved by keeping the Rollout** (closed; raised against the earlier Sandbox-CR revisions):
blue-green preserved, `replicas` works, no bespoke health policy needed, and the coder egress regime
does not apply — `30-networkpolicy.yaml` selects `agent-sandbox.io/role: coder`, a label an agent
pod does not carry, so in-cluster DNS to Bifrost and MCP servers is normal.

## 8. Validation plan

Steps 1–3 need no cluster.

1. **Zero-regression proof.** Render the modified CUE: the `sandbox: false` output must be
   **byte-diff-clean** against the current generated `agent.yaml`, and the `sandbox: true` output
   must differ by **exactly** `runtimeClassName` plus the two `securityContext` blocks.
2. **Prove the value flows:** `helm template … --set global.sandboxRuntimeClass=kata-fc` must print
   `kata-fc`. Testing with `kata-clh` proves nothing — it is the `values.yaml` fallback.
3. **Fail-closed:** empty `global.sandboxRuntimeClass` + `sandbox: true` must fail the render, not
   emit a pod without `runtimeClassName`.
4. **Non-sandbox e2e:** `example-agent-simple.yaml` — Rollout Healthy, agent reachable through the
   gateway, unchanged from before.
5. **Sandbox e2e on a Kata cluster:** `example-agent-sandbox.yaml` — pods on a node labelled
   `katacontainers.io/kata-runtime`, `runtimeClassName` honoured, scheduling force-merged, non-root
   (`id` returns non-zero uid), `/health` answering, reachable via the stable Service and HTTPRoute,
   and **in-guest DNS to Bifrost and each MCP server**, plus `gateway-identity` and EKS Pod Identity
   (§7.6).
6. **Blue-green e2e in the sandbox path** — the capability this design exists to preserve: push a new
   image tag, confirm the preview ReplicaSet of microVMs comes up, the preview Service resolves it,
   and promotion swaps normally.
7. **Negative:** a root-`USER` image + `sandbox: true` → clear startup failure, not a privileged pod;
   `sandbox: true` on a non-Kata cluster → pods `Pending`, Rollout not-Ready.
8. **`kubectl apply --dry-run=server`** on the regenerated ComponentDefinition.

### 8.1 Validation results

Run on a live platform deployment: EKS 1.35 Auto Mode hub plus two spokes, `us-west-2`, prefix
`peeks`, `agent_sandbox` + `agent_sandbox_kata` + `kata_nodepool` enabled, `vela` CLI 1.10.4,
Crossplane 2.2.1, kata-deploy 4.0.0. 7 of the 8 checks pass; one is **superseded** by a fix this
branch now carries.

| § | Check | Result |
|---|---|---|
| 1 | Zero-regression | **PASS.** Every hunk in the regenerated ComponentDefinition sits inside `if parameter.sandbox`, and the other 9 templates are byte-identical. Live: `example-agent-simple.yaml` renders **no** `runtimeClassName` and **no** `securityContext` at either level. |
| 2 | Value flows | **PASS.** `--set global.sandboxRuntimeClass=kata-fc` → `_sandboxRuntimeClass: "kata-fc"`; default → `"kata-clh"`. |
| 3 | Fail-closed | **PASS, and stricter than designed.** Empty class + `sandbox: true` is rejected at *admission* by the KubeVela validating webhook (`explicit error (_|_ literal) in source`), so the Application is never persisted — no Rollout, no unisolated runc pod. |
| 4 | Non-sandbox e2e | **PASS.** Rollout Healthy 3/3 on ordinary Auto Mode nodes. |
| 5 | Sandbox e2e on a Kata cluster | **PASS.** Karpenter provisioned a `kata-nested` node on demand (0 → 1, `c8i.2xlarge`); both replicas Running; **guest kernel `6.18.35` vs host `6.12.110-135.201.amzn2023.x86_64`**; the agent serves its A2A card over HTTP 200 from inside the microVM, reached from an ordinary pod via the ClusterIP Service. |
| 6 | Blue-green e2e in the sandbox path | **PASS.** A `properties` change drove revision 1 → 2: `Progressing` → `Paused` (preview gate) → `Healthy 2/2`. Both Services kept, and isolation survived promotion — new pods still `kata-clh`, still guest kernel `6.18.35`, still uid 1000. |
| 7 | Negative: root-`USER` image | **SUPERSEDED — see below.** |
| 8 | `kubectl apply --dry-run=server` | **PASS.** `componentdefinition.core.oam.dev/agent configured (server dry run)`. |

**Why §7 is superseded.** The plan expected a root-`USER` image to produce a clear startup failure.
That was correct when `sandbox: true` set `runAsNonRoot` with no `runAsUser`. It no longer holds:
because the component now supplies a numeric `sandboxRunAsUser` (default `1000`), a root-`USER`
image **starts successfully as uid 1000** rather than being rejected. Verified directly — a root
image under the rendered `securityContext` reported `uid=1000`, container Running.

That is the intended trade: the flag became purely additive for root images instead of refusing
them, and `runAsNonRoot` still rejects an explicit uid 0 at admission, so root is still
unreachable. The residual risk moves from "won't start" to "may misbehave": an image that genuinely
needs to write to root-owned paths at startup will now fail in its own way rather than with a clear
`runAsNonRoot` message. Note also that the group is not forced — the root image reported
`gid=0(root)` — which PSS `restricted` permits; adding `runAsGroup`/`fsGroup` is a possible
follow-up.

**Two defects found by this validation, fixed on this branch.**

1. A literal Helm action inside a CUE comment. `vela def render` copies comments verbatim into the
   generated ComponentDefinition, and Helm parses that file as a Go template before KubeVela sees
   it, so the comment was evaluated and failed with
   `parse error ...: unexpected <.> in operand`. That breaks the render of the **entire**
   `oam-agent-components` chart — all six ComponentDefinitions and all three TraitDefinitions — and
   was latent only because the generated template had not been regenerated.
2. The missing numeric `runAsUser` described above. Its severity was that this component's **own
   default image** (`public.ecr.aws/z0a4o2j5/strands-agent`, `USER appuser`, uid 1000) could not
   start under `sandbox: true`, and the component exposed no `securityContext`/`runAsUser`
   parameter, so an application developer had no way to work around it.

**Not covered.** Firecracker (`kata-fc`) was rendered but not run — its node layer is off on this
platform, as §7 of this document already records. `overhead.podFixed` was observed applied
(`{"cpu":"250m","memory":"130Mi"}`) but its effect on scheduling density was not load-tested.
