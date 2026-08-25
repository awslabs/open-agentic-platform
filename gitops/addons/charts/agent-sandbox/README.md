# agent-sandbox

**Flow A of the [Dark Factory pattern](../../../../docs/dark-factory/README.md).**
Hardware-isolated **Kata micro-VM sandboxes** (`agents.x-k8s.io` Sandbox CRD) with a **pre-warmed
pool** kept ready by a pool-manager controller — the reusable isolation substrate any agent
workload (notably the Dark Factory coding pipeline) can claim on demand.

## What it installs

| Template | Resource | Purpose |
|---|---|---|
| `10-runtimeclasses.yaml` | RuntimeClass ×N | `kata-clh` (default), `kata-qemu`, `kata-fc` — carry the nodeSelector **and tolerations** that steer a selecting pod onto the matching kata Karpenter pool, plus the per-VMM `overhead`. Owned here, not by `kata-deploy` (no toleration support there) |
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
| `kata.vmm` | `clh` | VMM the warm pool runs on — flip to `qemu` (same kata pool) or `fc` (Firecracker; needs the kata-fc pool + kata-deploy-fc). Selects a key of `kata.runtimeClasses`. |
| `warmPool.targetIdle` | `3` | Idle sandboxes kept ready |
| `warmPool.idleScaleToZeroSeconds` | `900` | Idle → `replicas:0` (PVC kept) |
| `warmPool.reapAfterSeconds` | `3600` | Reap abandoned claimed sandboxes |
| `coderTemplate.bifrostUrl` | `http://bifrost.bifrost.svc.cluster.local:8080` | LLM gateway (Bifrost, not LiteLLM) |

## Prerequisites

- **Kata runtime installed on nodes** (via `kata-deploy` + kata Karpenter pools from the base
  platform / `eks-platform-openclaw`).
- **Sandbox CRDs** (`Sandbox`, `SandboxTemplate`, `SandboxClaim`) applied at an earlier sync-wave
  from the upstream operator bundle (`public.ecr.aws/t6v6o5d5/agent-sandbox:v0.1.0`) — the ~4k-line
  OpenAPI schema is not vendored into this chart.
- **Bifrost** LLM gateway reachable at the configured URL.

## Notes

- The pool-manager is a CronJob (kubectl + jq reconcile loop) for a dependency-free reference
  implementation; swap for a real controller if reconcile latency matters.
- `warmPool.enabled=false` removes the pool-manager entirely (operator + RuntimeClasses + template
  remain, for manual/consumer-driven claims).
