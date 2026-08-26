# kata-nodepool

The **consuming layer** over the platform-repo `karpenter` controller addon: a
Karpenter `EC2NodeClass` + `NodePool` that provision the **nested-virt (Kata
micro-VM) nodes** EKS Auto Mode's Bottlerocket nodes cannot host.

This is the GitOps-managed replacement for the out-of-band self-managed MNG the
old [`agent-sandbox/nodepool/README.md`](../agent-sandbox/nodepool/README.md)
describes. Nothing is created out-of-band any more — subnets, the cluster
security group, and the node IAM role are **discovered from cluster tags**.

## How the pieces fit

```
enable_karpenter           → platform `karpenter` addon: controller + IAM
                             (KarpenterNodeRole-<cluster>, pod-identity), CRDs
enable_kata_nodepool  (here) → EC2NodeClass + NodePool (this chart)
enable_agent_sandbox_kata  → kata-deploy: the Kata runtime on the kata nodes
enable_agent_sandbox       → operator, RuntimeClasses, warm pool (the consumers)
```

All four flip from `gitops/overlays/environments/<env>/enabled-addons.yaml`.
`kata_nodepool` is **useless without `karpenter`** (no controller reconciles the
NodePool, and `KarpenterNodeRole-<cluster>` won't exist).

## Coexistence with Auto Mode

The self-managed and Auto Mode Karpenter controllers share the `karpenter.sh`
NodePool/NodeClaim CRDs but never fight, because each only reconciles NodePools
whose `nodeClassRef.group` matches its own NodeClass API:

| Controller    | `nodeClassRef.group` | NodeClass                        |
|---------------|----------------------|----------------------------------|
| Auto Mode     | `eks.amazonaws.com`  | `eks.amazonaws.com/NodeClass`    |
| self-managed  | `karpenter.k8s.aws`  | `karpenter.k8s.aws/EC2NodeClass` |

This NodePool sets `group: karpenter.k8s.aws`, so Auto Mode ignores it.
Validated live on spoke-dev (2026-07-23): kata pods pinned to this pool, normal
pods stayed on Auto Mode, NodeClaims partitioned with zero cross-ownership.

## Scheduling isolation

- **Taint** `kata=true:NoSchedule` — only pods tolerating it land here; normal
  pods stay on Auto Mode (Karpenter ignores them, its only pool being tainted).
- **Startup taint** `katacontainers.io/runtime-not-ready:NoSchedule` —
  self-removed by kata-deploy 4.0.0 once the runtime is installed, gating kata
  pods until the node is ready.
- **Label** `katacontainers.io/kata-runtime=true` (+ `kata-enabled`,
  `node-type`) stamped at node birth. The `kata-clh` RuntimeClass nodeSelector
  (admission-injected into every kata pod) requires it — it must exist before
  the node is Ready or cold start deadlocks. It is also what the pending
  warm-pool pods select.

## Discovery (no hardcoded IDs)

Set on the `discovery` values, matched against tags the platform VPC composition
/ EKS already apply — so the chart is portable across spokes with no per-cluster
IDs:

| Resource       | Tag(s) matched                                                            |
|----------------|---------------------------------------------------------------------------|
| private subnets| `platform.gitops.io/cluster: <cluster>` **+** `kubernetes.io/role/internal-elb: "1"` |
| cluster SG     | `aws:eks:cluster-name: <cluster>`                                         |
| node role      | `KarpenterNodeRole-<cluster>` (created by the `karpenter` addon)          |

`<cluster>` comes from the cluster-secret `aws_cluster_name` annotation, injected
by the registry entry (`gitops/addons/registry/sandbox.yaml`).

## Sync ordering

Sync-wave `1` (the node layer) — the NodePool exists before the warm pool
(wave 2) creates the pending pods that trigger provisioning. Both CRs carry
`argocd.argoproj.io/sync-options: SkipDryRunOnMissingResource=true` so the app
doesn't hard-fail its dry-run before the platform `karpenter` app has synced the
`karpenter.k8s.aws` CRD; the `_defaults` retry (limit -1) then converges it.

## The four pools

Mirrors elamaran11/eks-platform-openclaw on two axes — VMM (kata clh/qemu vs
Firecracker) and node type (nested-virt vs bare metal). Each toggles independently:

| Pool (`values`) | Default | VMM | Node | Taint / label | Runtime installer |
|---|---|---|---|---|---|
| `kataNested`  | **on**  | clh, qemu | nested-virt `c8i`/`m8i` | `kata` / `katacontainers.io/kata-runtime` | `kata-deploy` (`enable_agent_sandbox_kata`) |
| `kataMetal`   | off | clh, qemu | `*.metal`               | `kata` / `katacontainers.io/kata-runtime` | same `kata-deploy` |
| `kataFc`      | off | firecracker | nested-virt `c8i`/`m8i` | `kata-fc` / `katacontainers.io/kata-runtime-fc` | `kata-deploy-fc` (`enable_agent_sandbox_kata_fc`) |
| `kataFcMetal` | off | firecracker | `*.metal`               | `kata-fc` / `katacontainers.io/kata-runtime-fc` | `kata-deploy-fc` |

- **metal pools** are the fallback for their nested sibling (lower `weight`), for
  when nested-virt capacity is unavailable. They omit `cpuOptions` (native VT-x)
  and select an explicit `instanceTypes` list instead of family+vCPU-floor.
- **fc pools** need a **devmapper thin-pool** (Firecracker uses a block
  snapshotter, not overlayfs) built in node `userData`, plus a **separate
  `kata-deploy-fc`** release that maps `kata-fc → devmapper`. Enabling `kataFc*`
  without that runtime app leaves fc pods Pending.

> ⚠️ **Only `kataNested` (clh) is verified live on this platform** (spoke-dev
> 2026-07-23). `kataMetal`, `kataFc`, `kataFcMetal` are ported from openclaw and
> **unverified here** — the fc devmapper path especially. They ship disabled.

To swap the warm pool's VMM, set `kata.vmm` (clh|qemu|fc) on the **agent-sandbox**
chart — that steers the SandboxTemplate onto the matching pool. This chart only
provisions the nodes; agent-sandbox decides which pool the pool lands on.

## Cost / instance selection

Nested-virt `8i` and `*.metal` nodes cost more than the Auto Mode `c6*` defaults;
`consolidateAfter` + the warm pool's idle scale-down keep the bill proportional
to actual sandbox activity. All pools are **on-demand only** — the ported
`karpenter` controller has no SQS interruption queue, so a reclaimed spot node
wouldn't drain gracefully; add `spot` to `capacityTypes` only after wiring one.
