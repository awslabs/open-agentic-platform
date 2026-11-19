# agent-sandbox

**Flow A of the [Dark Factory pattern](../../../../docs/dark-factory/README.md).**
Hardware-isolated **micro-VM sandboxes** (`agents.x-k8s.io` Sandbox CRD) with a **pre-warmed
pool** kept ready by a pool-manager controller — the reusable isolation substrate any agent
workload (notably the Dark Factory coding pipeline) can claim on demand.

**Two isolation runtimes, selected by one value.** `runtime: nitro` (default) puts the warm pool on
**AWS Nitro Sandboxes** — a Firecracker micro-VM per pod, hardware-isolated by the Nitro
hypervisor, with no node layer of our own: on EKS Auto Mode (k8s 1.36+) the `isolated-sandbox` and
`confidential-sandbox` RuntimeClasses already exist and a sandbox-capable node is provisioned on
demand. `runtime: kata` is the original path, unchanged, and needs the nested-virt Karpenter pool
(`kata_nodepool`) plus `kata-deploy` (`agent_sandbox_kata`) first. Nitro is the default because both
of those ship **off** in every overlay in this repo, so a cluster that enables only this chart would
otherwise get a warm pool that cannot place a single pod. See
[docs/sandbox-agents/NITRO-DESIGN.md](../../../../docs/sandbox-agents/NITRO-DESIGN.md).

## What it installs

| Template | Resource | Purpose |
|---|---|---|
| `10-runtimeclasses.yaml` | RuntimeClass ×N | `kata-clh`, `kata-qemu`, `kata-fc` — carry the nodeSelector **and tolerations** that steer a selecting pod onto the matching kata Karpenter pool, plus the per-VMM `overhead`. Owned here, not by `kata-deploy` (no toleration support there). **The Nitro classes are NOT rendered here** and must not be: EKS owns them, and a second owner leaves both apps permanently OutOfSync |
| `20-sandboxtemplate.yaml` | SandboxTemplate | The coder pod spec the warm pool clones (isolation invariants baked in) |
| `30-networkpolicy.yaml` | NetworkPolicy | Default-deny egress → DNS + Bifrost + HTTPS only (breaks the lethal trifecta) |
| `40-poolmanager-rbac.yaml` | SA + Role + RoleBinding | Narrowly-scoped RBAC for the pool-manager |
| `41-poolmanager-cronjob.yaml` | CronJob | Reconciles the warm buffer: refill / scale-to-zero / reap |

## Enable

```yaml
# gitops/overlays/environments/{dev,prod}/enabled-addons.yaml
enabledAddons:
  agent_sandbox: true
```

The ApplicationSet cluster-generator (sync-wave 2) fans the chart onto any cluster carrying the
`enable_agent_sandbox` label. Installed on **spoke-dev and spoke-prod**; the Dark Factory pipeline
only *runs* on spoke-dev (prod pool stays dormant).

## Key values

| Value | Default | Purpose |
|---|---|---|
| `runtime` | `nitro` | Isolation runtime for the warm pool: `nitro` (AWS Nitro Sandboxes, no node layer needed on Auto Mode) or `kata`. Decides which of the two blocks below applies. |
| `nitro.mode` | `isolated` | Trust model: `isolated` (node trusted, workload not) or `confidential` (node untrusted too — encrypted overlay, exec/stdin denied from the host, attestation to KMS). `confidential` **requires** `nitro.cedarPolicy`, and denies the `exec` the Dark Factory coder flow depends on. |
| `nitro.cedarPolicy` | `""` | Base64 Cedar policy, attached as `sandbox.amazonaws.com/policy`. Pins the exact pod spec, so regenerate it (`scripts/nitro/gen-cedar-policy.sh`) whenever the rendered template changes — a new `coderTemplate.image` digest is enough. |
| `nitro.networkIsolated` | `false` | Boot each sandbox with **no network device** at all. Strictly stronger than the egress NetworkPolicy below, because the device is absent rather than filtered. Off for the coder, which needs GitHub and Bifrost. |
| `kata.vmm` | `clh` | VMM the warm pool runs on when `runtime: kata` — flip to `qemu` (same kata pool) or `fc` (Firecracker; needs the kata-fc pool + kata-deploy-fc). Selects a key of `kata.runtimeClasses`. |
| `warmPool.targetIdle` | `3` | Idle sandboxes kept ready |
| `warmPool.idleScaleToZeroSeconds` | `900` | Idle → `replicas:0` (PVC kept) |
| `warmPool.reapAfterSeconds` | `3600` | Reap abandoned claimed sandboxes |
| `coderTemplate.bifrostUrl` | `http://bifrost.bifrost.svc.cluster.local:8080` | LLM gateway (Bifrost, not LiteLLM) |

## Prerequisites

- **An isolation runtime the nodes can actually run.** On the default `runtime: nitro` path that
  means a cluster on **Kubernetes 1.36+** in a US commercial region, with the classes present
  (`kubectl get runtimeclass isolated-sandbox confidential-sandbox`). EKS Auto Mode needs nothing
  else; outside Auto Mode you need a Nitro Sandbox AMI and nodes launched with
  `EnclaveOptions.Enabled=true`. On `runtime: kata`, the **Kata runtime installed on nodes** (via
  `kata-deploy` + kata Karpenter pools from the base platform / `eks-platform-openclaw`).
- **Sandbox CRDs** (`Sandbox`, `SandboxTemplate`, `SandboxClaim`) applied at an earlier sync-wave
  from the upstream operator bundle (`public.ecr.aws/t6v6o5d5/agent-sandbox:v0.1.0`) — the ~4k-line
  OpenAPI schema is not vendored into this chart.
- **Bifrost** LLM gateway reachable at the configured URL.

## Notes

- The pool-manager is a CronJob (kubectl + jq reconcile loop) for a dependency-free reference
  implementation; swap for a real controller if reconcile latency matters.
- `warmPool.enabled=false` removes the pool-manager entirely (operator + RuntimeClasses + template
  remain, for manual/consumer-driven claims).
