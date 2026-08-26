# Flow D — Lambda MicroVM–backed Agent Sandbox (alternative substrate)

**Flow D is a second Flow-A substrate.** Where Flow A hands out **Kata micro-VM pods** on a
self-managed nested-virt EKS node group, Flow D hands out **AWS Lambda MicroVMs** provisioned by the
ACK `lambdamicrovms` controller and composed by a single **KRO `ResourceGraphDefinition`**. The Agent
Sandbox UX is unchanged: a consumer (notably **Flow B — Dark Factory**) creates a `SandboxClaim`, a
pod shows up, and the **same `dark-factory-coder`** runs its coding/testing loop — except the coder
executes inside a Lambda MicroVM instead of on the Kata node.

> **Flow C is reserved for other work** — this substrate is **Flow D**.

> **Why a second substrate?** Kata (Flow A) needs a dedicated nested-virt node group the platform owns
> and pays for while idle. Lambda MicroVM is a **serverless** micro-VM: no node group to run, per-claim
> lifecycle, sub-second warm starts, and a clean **platform-owns-the-image / app-owns-the-instance**
> split that maps directly onto the two ACK CRDs. Flow B can target either substrate with no pipeline
> change — it only ever sees the Agent Sandbox `SandboxClaim` contract.

> 🎨 Diagrams are editable draw.io — sources in [`src/`](./src/), rendered PNGs in [`img/`](./img/).
> *(Flow D diagram sources are added alongside the Flow A/B ones; see `src/flow-d-*.drawio`.)*

---

## D.1 — Substrate architecture (KRO RGD over ACK primitives)

The platform installs two controllers and one composition layer, then exposes **one** custom
resource to consumers:

- **Managed ACK** (EKS Capability) runs the **GA** controllers — `iam.services.k8s.aws` (Role) and
  `s3.services.k8s.aws` (Bucket) — that the MicroVM image + instance depend on.
- **Self-managed ACK** runs **only** the pre-GA `lambdamicrovms.services.k8s.aws` controller (its own
  Helm chart / ArgoCD addon), because Managed ACK bundles GA controllers only.
- **Managed KRO** (EKS Capability) runs the `ResourceGraphDefinition` engine.
- A single **`MicrovmSandbox` RGD** ties it all together: one CR expands into `MicrovmImage` +
  `Microvm` + IAM `Role`(s) + S3 `Bucket`.

```
consumer (Flow B / any agent)
        │  creates
        ▼
  MicrovmSandbox  (kro.run/v1alpha1 — the single abstraction)
        │  expands into
        ├── MicrovmImage        (lambdamicrovms.services.k8s.aws)   ── platform-owned inputs
        ├── S3 Bucket           (s3.services.k8s.aws)               ── image codeArtifact store
        ├── IAM Role (build)    (iam.services.k8s.aws)              ── MicrovmImage.buildRoleArn
        ├── IAM Role (exec)     (iam.services.k8s.aws)              ── Microvm.executionRoleArn
        └── Microvm             (lambdamicrovms.services.k8s.aws)   ── app-owned instance lifecycle
```

*Edit: `src/flow-d-substrate.drawio` → `img/flow-d-substrate.png`.*

---

## D.2 — Platform-owned vs app-owned split (inside one RGD)

The two ACK CRDs encode the ownership boundary the platform team and application teams care about;
the RGD schema surfaces each half to the right owner:

| Layer | Owner | ACK resource | Key fields |
|---|---|---|---|
| **Image / substrate** | Platform | `MicrovmImage` | `baseImageARN`, `buildRoleArn`, `codeArtifact.uri` (S3), egress connectors |
| **Instance / run** | App team | `Microvm` | `imageIdentifier`, `executionRoleArn`, `ingress/egressNetworkConnectors`, `idlePolicy` |

- **Platform** sets the image once (built **from the existing `dark-factory-coder` image** + its
  `entrypoint.js`, published to the S3 `codeArtifact` bucket) — declarative, ACK-managed, GitOps.
- **App teams / Flow B** create per-claim `Microvm` instances referencing that image, and own the
  instance lifecycle (`RunMicrovm` / `TerminateMicrovm`, idle policy) via the same claim they use today.

*Edit: `src/flow-d-ownership.drawio` → `img/flow-d-ownership.png`.*

---

## D.3 — The RuntimeClass shim (claim → pod → MicroVM)

A literal Kubernetes `RuntimeClass` (like `kata-clh`) maps to a **node-local containerd handler**.
Lambda MicroVM is a **remote AWS service**, so a true node-level RuntimeClass isn't possible without a
virtual-kubelet provider (a large Go runtime component — explicitly **out of scope**). Flow D uses a
**RuntimeClass-marked bridge pod** instead, preserving the exact Agent Sandbox UX:

```
SandboxClaim (Flow B injects DF_ISSUE_NUMBER, repo, branch — unchanged)
        │
        ▼
Sandbox → Pod from the `lambda-microvm` SandboxTemplate variant
        │   (bridge container; lands on a normal Auto-Mode node, NOT the kata pool)
        ▼
bridge applies a MicrovmSandbox (KRO) CR
        │
        ▼
Microvm RUNNING  ── runs the SAME dark-factory-coder entrypoint (node /app/entrypoint.js)
        │
        ├── bridge streams MicroVM logs → pod logs   (pod Running  ⇔ Microvm RUNNING)
        └── pod exit / claim teardown → TerminateMicrovm
```

To Flow B and the user this is identical to Flow A — "a sandbox pod appeared and ran the coder" — but
the coder actually executed in the Lambda MicroVM. Log streaming is straightforward; interactive
exec/attach passthrough is **best-effort** (full fidelity would need virtual-kubelet).

*Edit: `src/flow-d-shim.drawio` → `img/flow-d-shim.png`.*

---

## D.3a — Suspend / resume (Sandbox.operatingMode → MicroVM)

The Agent Sandbox CRD exposes `spec.operatingMode ∈ {Running, Suspended}` — the declarative
suspend/resume intent. But the ACK `Microvm` CR has **no suspend field**: its spec is create-time
only, `State` is status-only, and `suspend-microvm`/`resume-microvm` are **imperative SDK ops the ACK
controller deliberately does not reconcile**. So flipping `operatingMode` does nothing on its own — a
controller must translate intent into the SDK call.

Flow D closes that gap with a tiny always-on **`microvm-lifecycle`** reconcile loop (a ConfigMap
script on `alpine/k8s`, same pattern as the pool-manager — **no virtual-kubelet, no new image**):

```
Sandbox.operatingMode: Running   → Suspended :  aws lambda-microvms suspend-microvm --microvm-identifier <id>
Sandbox.operatingMode: Suspended → Running   :  aws lambda-microvms resume-microvm  --microvm-identifier <id>
```

- `<id>` (the `microvmID`) is resolved from the `MicrovmSandbox` (KRO) status; the loop is idempotent
  (stamps a `last-mode` annotation, acts only on transitions).
- The `MicrovmSandbox` is **kept** across suspend (the bridge's `preStop` detects `operatingMode:
  Suspended` and skips teardown), so the VM survives suspend/resume; it's deleted only on real claim
  teardown → `TerminateMicrovm`.
- Chosen over bridge `preStop` hooks alone because a reconcile loop is **robust to pod/node loss** and
  resume needs no live pod. This is the open-source **Sandbox-CRD-driven** suspend/resume you get with
  the MicroVM substrate.

*Edit: `src/flow-d-suspend-resume.drawio` → `img/flow-d-suspend-resume.png`.*

---

## D.4 — Future: when `lambdamicrovms` goes GA

`lambdamicrovms` is currently **pre-GA** (`v1alpha1`), so its controller is self-managed. When it
graduates to GA upstream, **Managed ACK adopts it automatically** — the self-managed chart is deleted
and the `MicrovmSandbox` RGD is **unchanged** (it references the same `lambdamicrovms.services.k8s.aws`
CRDs regardless of who runs the controller). The design deliberately keeps the RGD independent of the
controller install method so this migration is a one-line addon removal.
