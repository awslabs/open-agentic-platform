# Design: Dark Factories as OAM components on shared pipelines

| | |
|---|---|
| Status | Implementation in progress |
| Components | `dark-factory` OAM component, `dark-factory-templates` platform chart |

---

## 1. Summary

A team gets its own **Dark Factory** — GitHub issue in, reviewed and gated PR out — by
applying one short KubeVela `Application`:

```yaml
apiVersion: core.oam.dev/v1beta1
kind: Application
metadata: {name: acme-factory, namespace: dark-factory-acme}
spec:
  components:
    - name: acme-factory
      type: dark-factory
      properties:
        repositories: [{owner: acme, names: [api]}]
        webhook: {host: hooks.example.com}
```

The design splits a factory into two halves with different owners:

| Half | What | Scope | Owner |
|---|---|---|---|
| **Pipeline** | The run / merge / iterate workflows and the code their steps execute | **Cluster**: shared `ClusterWorkflowTemplate`s, versioned, with the step scripts inside them | Platform (`dark-factory-templates` chart) |
| **Factory** | Identities, credentials, warm pool of coder VMs, trigger, settings | **Namespace**: one factory per namespace | The team (`dark-factory` OAM component) |

A pipeline run always executes **in the factory's namespace**, under that namespace's
identity, with that namespace's secrets and VMs. The shared templates are definitions
only; they carry no credentials and no tenant data.

---

## 2. Background

The Dark Factory today is the `dark-factory` Helm chart (`gitops/addons/charts/dark-factory`):
one shared factory on the hub. Its pieces live in fixed namespaces (`argo`,
`argo-events`, `agent-sandbox-system`) and are configured once per cluster through
chart values.

A run, in either the chart or this design:

```
issue labeled ──► claim a warm Kata micro-VM
                  coder (in the VM) implements, tests, pushes df/issue-N, opens PR
                  gates post commit statuses:   holdout ∥ security   (+ DevOps App check)
                  release the VM
PR comment ─────► revision run with the comment as the request (bounded)
PR approved ────► merge ONLY if every required check is green, release the VM
```

The coder runs **model-written code prompted by text anyone can put in a public
issue**. Every decision below is constrained by keeping that code away from
credentials and the cluster control plane.

## 3. Goals and non-goals

**Goals**

1. A team can stand up a factory without platform involvement and without knowing the
   cluster's account, region, or network addresses.
2. Factories are isolated from each other and from the hub control plane.
3. Pipeline logic exists **once**, is owned by the platform, and can be upgraded
   safely.
4. The security properties of the existing chart are preserved or strengthened.

**Non-goals (this iteration)**

- Porting deploy-test, the sticky status comment, the claim reaper, metrics, or the
  Lambda MicroVM substrate from the chart.
- Migrating the chart's existing hub factory onto the shared templates (a natural
  follow-up, see [§9](#9-follow-ups)).
- Multiple factories in one namespace.

---

## 4. Architecture

```
                           ┌─────────────── cluster scope (platform) ───────────────┐
                           │ ClusterWorkflowTemplates  dark-factory-run-v1           │
                           │                           dark-factory-merge-v1         │
                           │                           dark-factory-iterate-v1       │
                           │   └ step scripts inside   merge/iterate/security/holdout│
                           │ ClusterNetworkPolicy      deny coder → control plane    │
                           └───────────────────────────▲────────────────────────────┘
                                                       │ workflowTemplateRef
                                                       │ (clusterScope: true)
 GitHub ──webhook──► Ingress ─► EventSource ─► Sensor ─┘
                    ┌──────────── namespace dark-factory-acme (team) ───────────────┐
                    │ Workflow pods ── run as SA dark-factory                       │
                    │   read  ConfigMap dark-factory-config   (settings)            │
                    │   read  Secret    dark-factory-github-orchestrator            │
                    │   claim SandboxWarmPool dark-factory-coder ─► Kata VM (coder) │
                    │   mount ConfigMap dark-factory-holdout  (hidden scenarios)    │
                    └───────────────────────────────────────────────────────────────┘
```

### 4.1 The contract

The shared templates find everything by **fixed names in the Workflow's own
namespace**. The component creates all of them. Anyone else who wants to run these
pipelines must create them too. The contract is defined once, in
`dark-factory-templates/values.yaml` → `contract`.

| Name | Kind | Purpose |
|---|---|---|
| `dark-factory` | ServiceAccount | Workflow identity: claims VMs, submits iterate runs |
| `dark-factory-executor` | ServiceAccount | Token-less identity for the holdout step |
| `dark-factory-coder` | SandboxWarmPool | The VMs a run claims |
| `dark-factory-github-orchestrator` | Secret (`token`) | Hub-side GitHub token |
| `dark-factory-config` | ConfigMap, label `workflows.argoproj.io/configmap-type=Parameter` | Per-factory settings |
| `dark-factory-holdout` | ConfigMap (optional), keys `<owner>-<repo>.json` | Hidden scenarios |

`dark-factory-config` keys: `coder-engine`, `bifrost-url`, `claim-ttl-seconds`,
`coder-timeout-seconds`, `holdout-{enabled,threshold,require-scenarios,judge-runs,judge-quorum,judge-model,blocking,timeout-seconds}`,
`security-{enabled,block-level,wait-seconds,timeout-seconds,check}`,
`devops-{enabled,check}`, `max-iterations`.

---

## 5. Critical decisions

Each decision lists the options considered, the choice, why, and what it costs.

### D1. Pipeline templates: cluster-scoped, not per namespace

**The central decision.** Where do the run / merge / iterate WorkflowTemplates live?

| | A. `ClusterWorkflowTemplate`, shared (**chosen**) | B. `WorkflowTemplate` rendered into every factory namespace |
|---|---|---|
| Copies of pipeline logic | One | One per factory, plus the chart's |
| Upgrade | Change once; every factory on that version gets it | Re-render every factory |
| Who can edit the pipeline | Platform only (cluster-scoped; tenants have no write RBAC) | Any namespace admin can edit or weaken their copy |
| Blast radius of a bad change | Every factory on that version | One factory |
| Per-factory variation | Parameters only | Anything |
| Where it runs | Factory namespace either way | Factory namespace |

**Why A.**

- **One pipeline, not N.** With B the workflow logic lives in CUE, once per factory
  at render time, *and* again in the chart's YAML. Those copies drift. A fix to the
  merge guard in one place silently misses the other. This was the largest
  maintenance cost of the first version of the component.
- **The pipeline is a security control.** The merge guard, the iteration cap and the
  fail-closed gate handling protect the repo. Tenants should be able to *use* them but
  not *edit* them. Cluster scope gives that separation for free, through RBAC.
- **Nothing per-factory actually has to live in the template.** Every factory-specific
  value fell into one of three groups, each with a clean answer (D2, D3, D5). Nothing
  forced a per-namespace copy.

**What it costs, and the mitigations.**

- *Blast radius.* A broken template breaks every factory pinned to it. Mitigated by
  **versioned names** (D4): incompatible changes ship as `-v2`, and factories opt in.
- *Less flexibility.* A factory can only vary what the template parameterizes. That is
  intended. A team that needs a different pipeline gets a new version, or its own
  templates written against the same contract.
- *Shared control plane object.* ClusterWorkflowTemplates are visible cluster-wide.
  They contain no secrets or tenant data, so visibility is harmless.

> **What it does *not* buy.** Anyone who can create Workflows in a factory namespace
> can still submit one with overridden parameters (for example `holdout-enabled=false`
> on a merge run), or an inline Workflow that doesn't use the templates at all. The
> shared templates stop tenants *editing* the pipeline. They don't stop a namespace
> admin *bypassing* it. The hard control against unreviewed merges is **GitHub branch
> protection** requiring the `dark-factory/*` statuses (see [§6](#6-security-model)).
> Argo's controller setting `templateReferencing: Strict` would also block inline
> Workflows cluster-wide. It's worth evaluating, but it affects every Argo user on the
> hub.

### D2. Per-factory settings: a ConfigMap the templates read

How does a shared template get *this* factory's timeouts, thresholds, model, and gate
switches?

| Option | Verdict |
|---|---|
| **Template parameters defaulted from `dark-factory-config`** via `valueFrom.configMapKeyRef` (**chosen**) | Argo resolves the ConfigMap in the **Workflow's** namespace, so one template serves every factory. Each trigger only supplies event data. |
| Sensor injects every setting as an argument | Works, but every trigger and `iterate.js` would have to carry ~20 settings. They'd duplicate and drift. |
| Bake settings into per-namespace templates | That's option D1-B. |

**Trade-offs.**

- Settings are read when a run *starts*, so a config change applies to the next run,
  not to running ones. That's acceptable and arguably correct.
- The ConfigMap needs the label `workflows.argoproj.io/configmap-type: Parameter`, or
  Argo ignores it. The component sets the label.
- The controller reads ConfigMaps with its own cluster-wide permission. Verified on
  `dora-showcase`: `argo-cluster-role` grants `configmaps get/list/watch`.
- Any explicitly passed argument overrides the ConfigMap. Same caveat as D1.

### D3. Gate on/off: runtime conditions, not rendering

In the per-namespace version, a disabled gate simply wasn't rendered. A shared
template has a fixed shape.

**Decision:** every gate step is always present, and skips itself with an Argo `when:`
on the config value (`holdout-enabled == true && pr-number != ""`). The merge step
reads the same switches to decide which statuses to require.

**Trade-off:** a disabled gate still appears as a skipped node in the Argo UI.
Cosmetic.

### D4. Versioning: immutable version families

**Decision:** templates are named `dark-factory-{run,merge,iterate}-<version>`, set by
the chart's `version` value. Each factory pins one (`pipelineVersion: v1`).
`iterate-v1` always submits `run-v1`, so a family never mixes versions.

- **Compatible fix** (a bug fix that doesn't change the contract): ship under the same
  version. All pinned factories get it.
- **Incompatible change** (new contract key, new step semantics): ship as a new
  version. Factories move by bumping their pin.

**Trade-off:** the chart renders one version at a time. To run `v1` and `v2` side by
side during a migration, deploy a second instance of the chart at the old revision.
That's more operational work than an in-place upgrade, and that's the point: it makes
breaking every factory at once a deliberate act.

### D5. Fixed contract names; one factory per namespace

Shared templates need stable names to resolve in the Workflow's namespace (D1).

| Option | Verdict |
|---|---|
| **Fixed names** (`dark-factory`, `dark-factory-coder`, …) (**chosen**) | Simple. Templates stay readable. A namespace holds one factory. |
| Names derived from a `factory` parameter | Allows several factories per namespace, but every name in every template becomes an expression. Some fields (template-level executor ServiceAccount, ConfigMap references used to *resolve* parameters) can't be safely templated. |

**Why one factory per namespace is the right unit anyway:** the namespace is
Kubernetes' isolation boundary for RBAC, Secrets, NetworkPolicy and quotas. Two
factories sharing one would share credentials and blast radius, which defeats the
purpose.

**Deviation from repo convention:** the OAM steering doc says a workload's
ServiceAccount is named `context.name` so identity traits attach to it. Here it's fixed
as `dark-factory`. That's deliberate: the workflow identity **must not** gain an AWS
identity (see D10), so not being a trait anchor is a feature.

### D6. Where the step code lives: inside the templates

The hub-side steps run four scripts copied from the `dark-factory` chart (`merge.js`,
`iterate.js`, `security-wait.js`, `evaluate.js`), plus `holdout-gate.sh`. The chart
mounts its copies from a ConfigMap filled with Helm's `.Files.Get`. That ConfigMap
lives in one namespace, and a ConfigMap can only be mounted in its own namespace, so
factories elsewhere can't use it.

| Option | Verdict |
|---|---|
| **Scripts embedded in the ClusterWorkflowTemplates, on a pinned stock image** (**chosen**) | The templates chart reads the script files with `.Files.Get` and places each one in the step's pod as an Argo **raw artifact** (`inputs.artifacts[].raw.data`, mounted at `/scripts/<file>`). Cluster scope carries them to every namespace. No image build. |
| Custom steps image (`examples/dark-factory/steps`) | The first implementation. It works, but needs a per-account build and push, and the image tag can drift from the template version. |
| ConfigMap in each factory namespace | The component would need the script text, and CUE can't read files. |
| Reuse the coder image | It runs inside the untrusted VM. Hub-side merge logic doesn't belong there. |
| Paste the scripts into the templates by hand | ~700 duplicated lines that drift from the chart. |

**Why embed.** The steps image only existed because the first version defined the
pipeline in CUE, which can't read files. Once the pipeline moved to a Helm chart
(D1), that reason went away. Embedding also gives two things the image can't:

- **The scripts *are* the template version.** `dark-factory-merge-v1` always runs
  the `merge.js` it shipped with, so the template and its code can't skew. With an
  image, templates `v1` and image tag `v0.1.0` are versioned separately and can
  drift.
- **One less artifact per account.** No Dockerfile, no ECR repository, no build step
  before a hub can run a factory.

**Raw artifacts, not script `source`.** Argo's `script.source` holds one script per
step. The holdout step needs two files (`holdout-gate.sh` calls `evaluate.js`). Raw
artifacts place any number of files at fixed paths and need no artifact repository,
so every step uses them the same way.

**The base image.** The steps need node, bash, curl, git (holdout clones the branch)
and python3 (holdout diff assembly). The full `node:20` image (Debian bookworm) ships
with all of them, so no tools are installed at runtime. The scripts' few uses of `jq`
are rewritten in node, because `jq` isn't included. The image is a chart value
(`baseImage`), **pinned by digest**, and the registry entry points it at an ECR
pull-through cache, so hub nodes never pull straight from Docker Hub.

**Single source for the scripts.** Helm's `.Files.Get` only reads files inside the
chart being rendered, so the scripts move to `dark-factory-templates/files/`. The hub
`dark-factory` chart still mounts them from its own ConfigMap. To keep one copy, its
`scripts/` directory becomes a **symlink** to the new location. Helm and ArgoCD both
follow symlinks that stay inside the repository. This must be confirmed on the hub's
ArgoCD before the old copy is deleted. If it doesn't hold, the fallback is a second
copy until follow-up 1 moves the hub factory onto these templates.

**Trade-offs.**

- **Image size.** The full `node:20` image is about 1 GB, against about 150 MB for
  the custom image. It's pulled once per hub node and cached.
- **Template size.** Each template carries its scripts, under 50 KB per template,
  well within the object size limit.
- **Supply chain.** It's a public base image either way (the custom image was built
  `FROM node:20-alpine`). Pinning by digest and pulling through ECR keeps it
  reproducible and scannable.
- **Editing the code still means a template change.** That's the point: only the
  platform can change step code, through the same review and versioning as the
  pipeline (D1, D4).

*History:* the first implementation shipped the custom steps image. It's retired by
this decision, and the remaining work is listed in [§9](#9-follow-ups).

### D7. Control-plane isolation: a platform policy keyed on a pod label

On EKS VPC-CNI, an ordinary NetworkPolicy does not reliably apply to Sandbox-owned
pods. The real isolation is an Admin-tier `ClusterNetworkPolicy`, which tenants can't
override. The chart's existing policy selected **only** `agent-sandbox-system`.

| Option | Verdict |
|---|---|
| **Second platform ClusterNetworkPolicy selecting `agent-sandbox.io/role=coder` pods in any namespace** (**chosen**) | Every factory is covered automatically. |
| Each factory renders its own ClusterNetworkPolicy | A tenant Application would own admin-tier, cluster-wide network policy. |
| Factories share `agent-sandbox-system` | Gives up per-namespace isolation (D5). |

**Why it's safe to key on a label:** a pod that gains the label only gains
restrictions. The original namespace-scoped policy is untouched, because it covers
every pod in that namespace, labeled or not. The component also keeps a namespaced
egress policy (DNS, Bifrost, public HTTPS) as defense in depth.

### D8. Event plumbing: per namespace

| Option | Verdict |
|---|---|
| **EventBus, EventSource, Sensor and Ingress in each factory namespace** (**chosen**) | Each factory receives only its own repos' webhooks with its own HMAC secret. A noisy or broken factory can't affect another. |
| One central EventSource/Sensor routing events to namespaces | Fewer components, but one service holding every factory's webhook secrets and a routing table to keep in sync. A single point of failure and of compromise. |

**Trade-off:** a JetStream EventBus per namespace (3 small pods). It can be shared by
setting `eventBus.create: false` if a namespace already has one. Webhook paths include
the namespace (`/dark-factory/<ns>/<name>`), so factories can share one ALB host
without colliding.

### D9. Credentials: three tokens per factory, from Secrets Manager

**Decision:** three ExternalSecrets per factory from the `aws-secrets-manager`
ClusterSecretStore, at `<cluster>/dark-factory/<namespace>/<name>/github/{coder,orchestrator,events}`.

| Token | Where it's used | Scope |
|---|---|---|
| coder | Mounted into the VM (0400, never in env) | Issues read; contents, PRs, statuses write. **No** merge path, no Issues write |
| orchestrator | Hub-side steps only | Adds Issues write and merge |
| events | EventSource only | Webhook registration and HMAC |

**Why:** the most exposed component (the VM) holds the least-privileged token, and
each factory's credentials live in its own Secrets Manager paths and its own namespace.
The cluster prefix lets each cluster's External Secrets identity be limited to its own
paths.

**Trade-off:** three secrets to seed per factory. `setup-secrets.sh` still writes the
chart's single-factory paths.

### D10. No AWS identity anywhere in a factory

None of the factory's ServiceAccounts carries IAM. The security gate waits for the
Security Agent **GitHub App** and mirrors its verdict, instead of calling AWS APIs. The
chart's alternative mode (IRSA plus S3 staging) is not ported.

**Why:** the holdout step executes coder output outside the VM, and every step shares
the namespace. Not having AWS credentials in the namespace at all is simpler and
stronger than scoping them per step.

**Trade-off:** the security gate depends on the GitHub App being installed on each
repo.

### D11. Holdout scenarios: one optional ConfigMap per factory

**Decision:** the team creates `dark-factory-holdout` in the factory namespace, one key
per repo (`<owner>-<repo>.json`). The run template mounts it read-only, marked
optional.

- **Kept out of the Application.** Scenarios are the coder's hidden test set. If they
  lived in the Application, and the Application lived in the target repo, the coder
  could read them.
- **One ConfigMap, keyed per repo**, because a shared template can't have a variable
  number of volumes. (The evaluator never read the chart's `rubric.md`, so dropping it
  loses nothing.)
- **Fails closed.** A repo with no key gets a `NOT VERIFIED` failure while
  `requireScenarios` is true, the default.

**Trade-off:** a ConfigMap is limited to 1 MiB, which is ample for scenario JSON. The
holdout step runs coder code, so it runs with **no Kubernetes token** and a token-less
executor identity. It still holds the orchestrator token to post its status (as in the
chart). Running the evaluator itself in a sandbox is a follow-up.

### D12. DevOps gate enforced at merge, not polled

The chart's `devops-gate` step polled the DevOps Agent App's check-run only to feed
the sticky status comment, which isn't ported. `merge.js` already reads that check-run
directly, and treats a missing check as a refusal.

**Decision:** no DevOps step. The check is required at merge (`devops-check` config).

**Trade-off:** the run finishes before the DevOps verdict exists. The verdict is
visible on the PR rather than in the Argo UI.

### D13. Platform-supplied environment values

Developers never set account, region, cluster name or Bifrost's address. They flow from
the cluster secret's annotations, through the addon registry, to Helm globals
(`awsAccountId`, `awsRegion`, `clusterName`, `bifrostUrl`, `bifrostClusterIp`), and then
into CUE parameter defaults. Bifrost is reached by **ClusterIP** (Kata guests can't
resolve cluster DNS), and admitted by a `/32` egress rule, because VPC-CNI matches
egress on the pre-DNAT ClusterIP.

**Trade-off:** `172.20.0.100` is now referenced in one more registry entry. The
existing "grep 172.20.0.100" sync note covers it.

### D14. Smaller choices

| Choice | Reason |
|---|---|
| `synchronization.mutexes` (list) | Argo v4 removed `mutex`. Verified: v4.1.4 rejects it. The list form works on v3.6+. |
| No issue title in the claim | Raw issue text in the claim manifest broke on quotes. The coder reads the title from the issue it fetches. |
| Revision notes base64-encoded | Arbitrary markdown can't break the claim manifest. |
| Deterministic Workflow names per event | GitHub redelivers webhooks. Duplicates collide (`AlreadyExists`) instead of racing. |
| Registry gate `enable_dark_factory` | One label already turns on the Dark Factory stack. The templates join it. |

---

## 6. Security model

| Threat | Control |
|---|---|
| Coder code reaches credentials | Kata VM (own kernel); no Kubernetes token, no IAM; only a narrow GitHub token at `/etc/secrets` (0400) |
| Coder code reaches the hub control plane | Admin-tier ClusterNetworkPolicy (D7). Namespaced egress allows only DNS, Bifrost and public HTTPS |
| Coder games its own tests | Holdout scenarios it can't see (D11); a judge from a different model family; fail-closed on missing scenarios |
| Coder output executed on the hub (holdout step) | `npm install --ignore-scripts`; no Kubernetes token; no AWS identity in the namespace |
| Unreviewed or failing code merged | Merge only on a human approval event; `merge.js` refuses unless every required status is green at the PR head; a missing status is a refusal |
| Tampered step code | Step scripts live inside the cluster-scoped templates (D6); only the platform can change them, and every change ships as a template version |
| Tenant weakens the pipeline | Templates are cluster-scoped and platform-owned (D1). **Required:** GitHub branch protection on each repo requiring `dark-factory/*` checks, because a namespace admin can still bypass the templates |
| Runaway revision loop | `maxIterations` cap; factory-authored comments filtered at the Sensor and in `iterate.js` |
| Cross-factory interference | One factory per namespace (D5); per-namespace events and secrets (D8, D9); Argo locks are namespace-scoped |

**Residual risks**

- Branch protection is the only hard stop on self-merge. It's unavailable on private
  repos on GitHub's free plan.
- The coder's token is a standing credential. Short-lived, per-run GitHub App tokens
  would be stronger (noted in the chart too).
- The holdout step holds the orchestrator token while running coder code. It should
  eventually run sandboxed.

---

## 7. Deployment and operations

**Platform, once per hub**

1. Make the pinned base image (`baseImage`, a `node:20` digest) reachable through the
   ECR pull-through cache. Nothing to build.
2. Set `enable_dark_factory=true` on the cluster. That syncs `dark-factory-templates`
   (registry `sandbox.yaml`) plus the agent-sandbox stack.
3. Enable `enable_oam_components` so the `dark-factory` component type is registered.
   First regenerate its YAML with `platform/oam/generate.sh` against the hub.

**Team, per factory**

1. Seed the three Secrets Manager secrets.
2. Create `dark-factory-holdout` with a key per repo.
3. Install the Security Agent and DevOps Agent GitHub Apps on each repo (or disable
   those gates), and enable branch protection requiring the `dark-factory/*` checks.
4. Apply the Application.

**Upgrading the pipeline**

- Compatible change: update the chart and keep `version`.
- Incompatible change: bump `version`. Keep the old family running as a second chart
  instance until every factory has bumped `pipelineVersion`.

---

## 8. Validation

Done:

- CUE type-checks with defaults, with every gate off, and with the example
  Application.
- The CUE source goes through Helm with non-default values (account, region,
  cluster): every value flows through and no template text is left behind.
- **Contract check:** every `configMapKeyRef` key in the chart exists in the rendered
  `dark-factory-config`. Every contract name exists. Every Sensor reference resolves
  to a template in the chart.
- `helm lint` and `argo lint` (v4.1.4) pass on the three ClusterWorkflowTemplates.
- Server-side dry run on `dora-showcase`: the 3 templates, and 15 of the component's
  19 resources. The Sandbox and ExternalSecret CRDs aren't installed there.
- The holdout evaluator, run against sample scenarios with a stub judge that always
  says yes: an honest implementation passed 4/4, a hard-coded one failed 2/4. This
  ran in the first implementation's steps image. It must be re-run on the stock base
  image once D6 is implemented.

**Not yet done** (needs a hub with the sandbox operator):

- A live end-to-end run: label → VM → PR → gates → merge.
- Live confirmation that `configMapKeyRef` defaults resolve for a Workflow submitted
  with `workflowTemplateRef.clusterScope: true`. Argo documents this and the
  controller's RBAC allows it, but it hasn't been observed.
- The real judge model, and both GitHub Apps.
- **D6 (pending implementation):** confirm the stock `node:20` image has every tool the
  steps use, that raw artifacts place the scripts correctly, and that ArgoCD renders
  the `dark-factory` chart through the `scripts/` symlink.

## 9. Follow-ups

1. **Move the chart's hub factory onto the shared templates.** That removes the last
   duplicated pipeline. It needs the chart's extra steps (deploy-test, sticky status,
   Lambda path) ported first.
2. **Port the chart's templates to `mutexes`.** They fail on Argo v4 today.
3. **Evaluate `templateReferencing: Strict`** on the hub's Argo controller.
4. **Short-lived GitHub App tokens** per run, instead of standing tokens.
5. **Sandbox the holdout evaluator.**
6. **Update `setup-secrets.sh`** for per-factory paths.
7. **Implement D6:**
   - move the scripts into `dark-factory-templates/files/`;
   - embed them in the templates as raw artifacts;
   - replace `jq` with node in the scripts;
   - add the `baseImage` value and its registry entry;
   - symlink the `dark-factory` chart's `scripts/` to the new location;
   - delete `examples/dark-factory/steps/` and the `stepsImage` value.

## Appendix: files

| Path | Role |
|---|---|
| `platform/oam/definitions/components/dark-factory.cue` | The component (per-namespace half) |
| `platform/oam/examples/example-dark-factory.yaml` | Example Application |
| `gitops/addons/charts/dark-factory-templates/` | Shared ClusterWorkflowTemplates and the contract. After D6, also `files/` with the step scripts |
| `gitops/addons/registry/sandbox.yaml` → `dark-factory-templates` | Registers the chart. Templates `stepsImage` today, `baseImage` after D6 |
| `examples/dark-factory/steps/` | Steps image and `holdout-gate.sh`. **Retired by D6** |
| `gitops/addons/charts/agent-sandbox/templates/31-clusternetworkpolicy.yaml` | Second Admin-tier policy, keyed on the coder label |
| `gitops/addons/charts/oam-agent-components/values.yaml`, `registry/agentcore.yaml` | `bifrostUrl` / `bifrostClusterIp` globals |
| `gitops/addons/charts/dark-factory/scripts/iterate.js` | `WF_TEMPLATE` / `WF_CLUSTER_SCOPE` (defaults unchanged) |
| `examples/dark-factory/coder/entrypoint.js` | Title taken from the fetched issue |
