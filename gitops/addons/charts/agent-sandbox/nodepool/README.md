# Kata node pool provisioning

> ## ⚠️ SUPERSEDED — historical reference only
>
> **Nothing described below is rendered any more.** The Crossplane MNG path ("Route A")
> was removed: templates `15-kata-readiness`, `16-kata-launch-template`,
> `17-kata-nodegroup` and `18-kata-eks-addons`, the chart's `nodepool` values block,
> and the `kataUserData` helper are all deleted, and the per-cluster overlay this doc
> tells you to edit no longer exists either.
>
> **The current path is the [`kata-nodepool`](../../kata-nodepool/README.md) chart** —
> a Karpenter `EC2NodeClass` + `NodePool` over the platform `karpenter` addon, which
> discovers subnets, the cluster security group and the node IAM role from cluster
> tags instead of hardcoding them per cluster. Enable it with `kata_nodepool: true`.
>
> Kept because the coexistence findings below (Auto Mode + self-managed nodes,
> `/dev/kvm`, nested-virt instance families) still hold and are referenced from
> `kata-nodepool/README.md` and `docs/dark-factory` §12a. The `.tf` / `eksctl` /
> nodeadm artifacts it mentions were removed too.

The `agent-sandbox` chart installs the Sandbox operator, RuntimeClasses, coder
template, and pool-manager — but Kata needs **hardware-virtualization nodes** to
actually run micro-VMs. On an **EKS Auto Mode** cluster (like the spokes), Auto
Mode's managed Bottlerocket nodes can't host Kata, so we add a **self-managed
nested-virt Managed Node Group** alongside Auto Mode. Coexistence + `/dev/kvm`
were validated by a live spike (see [`docs/dark-factory` §12a](../../../../../docs/dark-factory/README.md)).

## ✅ Now GitOps-managed (Crossplane) — this is the default path

The node group is a **first-class GitOps resource** — ArgoCD owns the node-group
INFRA end-to-end, no out-of-band `eksctl`/`terraform apply`. It's rendered by the
chart's `templates/` as Crossplane managed resources:

| Template | Resource |
|---|---|
| `../templates/16-kata-launch-template.yaml` | Crossplane `LaunchTemplate` — `cpuOptions.nestedVirtualization=enabled` + nodeadm userData |
| `../templates/17-kata-nodegroup.yaml` | Crossplane `Nodegroup` — scale-to-zero, kata taint/labels, LT ref |
| `../templates/18-kata-eks-addons.yaml` | Crossplane `Addon` — vpc-cni + kube-proxy (the two Auto-Mode prerequisites) |

**Enable it** — the chart default (`../values.yaml`) keeps `nodepool` cluster-agnostic;
the hub-specific coordinates live in the **per-cluster overlay** the addon
ApplicationSet already layers on (`valueFiles: clusters/<name>/addons`):

```
gitops/addons/clusters/hub/addons/agent-sandbox/values.yaml
```

```yaml
nodepool:
  enabled: true          # provision/adopt the kata MNG
  manageAddons: true     # also adopt vpc-cni + kube-proxy (skip if base platform owns them)
  clusterName: hub       # + clusterEndpoint / clusterCA / serviceCidr, subnetIds,
                         #   nodeRoleArn, amiId, instanceType, launchTemplateId, region
```

> `clusterEndpoint` + `clusterCA` are **not secrets** — the CA is the cluster's
> PUBLIC api-server certificate (no private key) and the endpoint is public DNS;
> both ship in every kubeconfig. They live in the overlay (not the chart default)
> for reusability. They're baked into the LaunchTemplate `userData` at Helm render
> time, so a k8s Secret/env can't feed them. Dropping the custom `amiId` (letting
> EKS auto-inject the bootstrap) would remove them from git entirely — a possible
> future refactor.

Requires the Crossplane AWS providers `provider-aws-eks` + `provider-aws-ec2`
(installed on the hub) and a `ProviderConfig` (default: `default`).

**Adoption:** on a cluster that already has a kata node group / LT / addons (e.g.
from the earlier manual path), the templates carry `crossplane.io/external-name`
annotations that **import the existing resources in place** — no recreate, no node
churn. Set `launchTemplateId` + `nodegroupName` to the existing ids.

## Reference files (superseded by the Crossplane templates above)

Kept for reference / non-GitOps or air-gapped setups; **not** applied by ArgoCD:

| File | Purpose |
|---|---|
| `kata-mng-eksctl.yaml` | eksctl `ClusterConfig` to add the kata MNG (declarative, simplest) |
| `kata-mng.tf` | Terraform launch template (`cpu_options.nested_virtualization=enabled`) + MNG. `terraform validate` passes. |
| `kata-mng-launch-template-userdata.mime` | The AL2023 **nodeadm MIME** userData (modprobe kvm_intel + join). |

## ✅ PROVEN END-TO-END on EKS Auto Mode (spoke-dev, 2026-07-10)

Kata micro-VMs **do run on an Auto Mode cluster** via a self-managed nested-virt MNG.
Verified with a pod under `runtimeClassName: kata-clh`:

| | Value |
|---|---|
| Pod kernel (`uname -r` inside) | **`6.18.35`** — the Kata guest kernel |
| Host node kernel | `6.12.90-…amzn2023` |

Different kernels ⇒ the pod ran in a **real VM with hardware isolation**, not a container.

### The two Auto-Mode-specific prerequisites (the crux)

Auto Mode's built-in networking applies ONLY to its own managed nodes. A self-managed
MNG node has **neither the CNI nor kube-proxy** that pods need — so you MUST install
both EKS addons, or pods on the kata node can't reach the API server:

```
aws eks create-addon --cluster-name <cluster> --addon-name vpc-cni    --resolve-conflicts OVERWRITE
aws eks create-addon --cluster-name <cluster> --addon-name kube-proxy --resolve-conflicts OVERWRITE
```

- **Without `vpc-cni`** → node stays `NotReady` (`cni plugin not initialized`).
- **Without `kube-proxy`** → the node has no iptables rules for the `kubernetes.default.svc`
  (172.20.0.1) service IP, so **kata-deploy crashloops** with `Failed to get node ...
  client error (Connect)` — it connects to the API via the in-cluster service and times
  out. This was the real blocker (NOT the containerd restart, and not the nydus
  snapshotter — both were red herrings). Installing kube-proxy fixed it; kata-deploy then
  reached `1/1 Running` with **zero restarts** and installed the runtime cleanly.

Both `aws-node` and `kube-proxy` tolerate all taints (`operator: Exists`), so they land
on the tainted kata node automatically.

### The startup-taint gate (from openclaw PR #10)

The kata node registers with **two** taints:
- `kata=true:NoSchedule` — workload taint (only kata pods run here)
- `katacontainers.io/runtime-not-ready=true:NoSchedule` — **startup taint**; blocks all
  workloads until the runtime is installed. Set via nodeadm
  `--register-with-taints`. The **`kata-readiness` DaemonSet** watches kata-deploy's
  `/readyz` and removes this taint once install completes — proven to work here
  (`node ... untainted` in its log).

### IAM access-entry gotcha (lesson from this test)

If you recreate the node IAM role, its principal ID changes — **delete and recreate the
EKS access entry** (`type EC2_LINUX`) or the node's kubelet gets `Unauthorized` and never
registers. A stale access entry pointing at an old role ID is silent and confusing.

## Enablement sequence (per kata-capable cluster, e.g. spoke-dev)

1. **Provision the kata MNG** — apply the eksctl or Terraform manifest here. Nodes
   come up tainted `kata=true:NoSchedule`, labeled `kata-enabled=true`, with
   `/dev/kvm` (nested-virt) and `min=0` scale-to-zero. On Auto Mode, ensure the
   `vpc-cni` addon is installed (see prerequisite above) or the node stays NotReady.
2. **Install the runtime** — label the cluster secret `enable_agent_sandbox_kata=true`
   so the `kata-deploy` ArgoCD app (sync-wave 1) installs the containerd handlers.
3. **Install the capability** — label `enable_agent_sandbox=true` so the
   `agent-sandbox` app (sync-wave 2) installs the operator + RuntimeClasses +
   template + pool-manager.
4. **Enable the pool** — `warmPool.enabled=true` (default) pre-warms idle sandboxes.

> Both labels are set via the environment overlays (`gitops/overlays/environments/dev`).
> They are commented out today until a kata MNG exists on the spokes.

## Cost note

Nested-virt `c8i`/`m8i` nodes are more expensive than the `c6*` Auto Mode
defaults. `min=0` scale-to-zero + the pool-manager's idle scale-down keep cost
proportional to actual sandbox activity — you pay for kata nodes only while a
sandbox is claimed/warming.

## Teardown (spike lesson #2)

Delete the **MNG first and let it drain** (set `min/desired=0` beforehand). Don't
terminate the instance out from under the MNG — the ASG respawns and can wedge
the delete on a `Pending:Wait` lifecycle hook. Recover with
`aws autoscaling terminate-instance-in-auto-scaling-group` +
`complete-lifecycle-action`.
