# Design: Dark Factories as OAM components

| | |
|---|---|
| Status | Proposed; implementation in progress |
| Components | `dark-factory` OAM component, `dark-factory-templates` platform chart |

---

## 1. Summary

A **Dark Factory** takes a GitHub issue in and produces a reviewed, gated PR out. A team
gets one by applying a KubeVela `Application` that describes **its own delivery process**,
built from **its own steps**:

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
        process:
          stages:
            - {name: build,  steps: [{use: platform/code}]}
            - name: verify
              steps:
                - {use: platform/holdout, config: {threshold: 0.9}}
                - {use: acme/contract-tests}      # the team's own step image
                - {use: acme/licence-check}
            - {name: review, steps: [{use: platform/approval}]}
            - {name: release, steps: [{use: platform/merge}]}
```

Teams do not share one SDLC, and they do not share one toolchain. They differ on which
checks gate a change, in what order, where humans approve — and on *what a check even is*.
A team's verification may be a contract-test harness, a licence scanner, an internal
compliance tool, or a model-graded rubric nobody else has. A platform offering a fixed
vocabulary of checks still dictates the process, just at a finer grain.

So **teams own their steps**: a step is a container image the team builds, versions and
ships on its own cadence against a published contract (§3.1). The platform runs a
**registry** for discovery and reuse, but a team is never blocked waiting for us to add a
step type.

What the platform keeps is narrow and deliberate:

| Layer | Owner |
|---|---|
| **The merge decision** — whether a PR may merge, and which statuses are required | **Platform.** Not authorable by a team, at any tier. |
| **Isolation** — where step code runs, what it reaches, what credentials exist | **Platform.** Team steps run sandboxed with no credentials. |
| **Guardrails** — minimums no process may go below, per environment | **Platform.** |
| **Steps** — what each step does | **The team** |
| **Process** — stages, order, approvals, failure handling | **The team** |

It is tempting to reserve more — to fix the pipeline's shape, or reserve all step code,
and call both security controls. Neither is. A fixed shape is bypassable by any namespace
admin who can create a Workflow, so branch protection is the real control; and reserving
*all* step code conflates the few steps that decide mergeability with the many that merely
report a verdict.

That leaves the central claim: **we already execute untrusted code safely** — Kata
micro-VM, no Kubernetes token, no IAM, default-deny egress. A team's step image on that
same substrate, with no credentials and no merge authority, is strictly less dangerous
than what the factory already runs: model-written code prompted by text anyone can put in
a public issue. Running it costs nothing new, and it is what turns the factory from a
pipeline into a platform.

---

## 2. Scope

The coder runs model-written code from public issue text; team steps run team-written
code. Every decision below keeps both away from credentials, the merge decision, and the
cluster control plane.

**Building:** the step contract (§3.1) and registry addressing; team-authored steps
(digest-pinned images, sandboxed, platform-posted status); platform steps `code`,
`changeset`, `ci-check`, `holdout`, `security-scan`, `approval`, `merge`; a `process`
schema with presets and explicit stages (`onFail`, `skipWhen`); `factoryPolicy`
guardrails; and the per-namespace factory — identities, three scoped tokens, warm pool,
holdout source, settings, webhook trigger, status.

**Not building**

| | Why / instead |
|---|---|
| The `shared/` registry tier | Teams reference their own steps directly; promotion needs a review workflow and an owner (D3) |
| Signature verification of step images | Digest pinning and an allowed-registry list ship now; cosign is a follow-up |
| A warm pool for step execution | Steps run as Kata pods on demand; pool once real durations are known (D4) |
| `deploy`, environment promotion | Nothing needs it before merge gating works |
| Pluggable intake | `issue-labeled` with a configurable label; issue types and slash commands are additive |
| Autonomy 3.5 (low-risk auto-merge) | Targets the README's Level 3; needs `changeset` proven first |
| Multiple factories per namespace | D12 |
| Chart parity: sticky comment, claim reaper, metrics, Lambda substrate | Follow-ups; none gate a first factory |

---

## 3. Architecture

```
       ┌──────────────────── cluster scope (platform) ─────────────────────┐
       │ PLATFORM STEPS (ClusterWorkflowTemplates, scripts inside)         │
       │   platform/{code,changeset,ci-check,holdout,                      │
       │             security-scan,approval,merge}                         │
       │   └ merge is the ONLY arbiter of mergeability — never team-ownable │
       │ STEP RUNNER   df-step-runner-v1  ← executes any team step image    │
       │ GUARDRAILS    factoryPolicy (chart values, per environment)        │
       │ ClusterNetworkPolicy   deny coder + team steps → control plane     │
       └─────────────────────────────▲────────────────────────────────────┘
                                     │ templateRef (clusterScope: true)
                                     │ step contract (§3.1)
      ┌──────────── namespace dark-factory-acme (team) ───────────────────┐
      │ PROCESS  WorkflowTemplate dark-factory-process  ← rendered         │
      │                                                                    │
GitHub ─webhook─► Ingress ─► EventSource ─► Sensor ─► Workflow             │
      │ Workflow pods ── run as SA dark-factory                            │
      │   read  ConfigMap dark-factory-config                              │
      │   read  Secret    dark-factory-github-orchestrator  (platform only)│
      │   claim SandboxWarmPool dark-factory-coder ─► Kata VM (coder)       │
      │   mount Secret    dark-factory-holdout  (hidden scenarios)         │
      │   TEAM STEPS ─► Kata pod, no tokens, declared egress only          │
      └────────────────────────────────────────────────────────────────────┘
```

### 3.1 The step contract

A **published ABI**, not an internal convention: teams build against it without reading
our templates. A step is an OCI image whose entrypoint reads the environment and writes
one result file.

| Env in | |
|---|---|
| `DF_REPO` | `owner/name` |
| `DF_ISSUE_NUMBER` | The originating issue; the run key (D21) |
| `DF_PR_NUMBER` | Empty before `platform/code` opens the PR |
| `DF_BRANCH`, `DF_BASE_BRANCH` | `df/issue-<n>`, and the PR base |
| `DF_STEP_NAME` | This instance's name; namespaces its status context |
| `DF_CONFIG` | JSON object of this step's `config` from the Application |
| `DF_WORKSPACE` | The checked-out branch, if the step requested it |
| `DF_RESULT` | Path the step must write its result to |

Out — one JSON file at `$DF_RESULT`:

```json
{"status": "success|failure|skipped", "summary": "one line", "details": "optional markdown"}
```

A non-zero exit with no result file is a `failure` carrying the last log line. A step
**never posts its own GitHub status**: the runner reads the result and the platform posts
it, which keeps "a missing status is a refusal" true and stops a step forging another
step's check.

**Capabilities**, requested in the Application and granted narrowly: `workspace: true`
(the branch at `$DF_WORKSPACE`, discarded after), `egress: [<host>...]` (added to the
step's policy; default deny-all but DNS), `secrets: [<name>...]` (keys from the factory's
`stepSecrets/` prefix, read-only).

Nothing else is reachable. There is no capability granting the orchestrator token, the
holdout scenarios, a Kubernetes token, AWS credentials, or merge authority — those are
absences, not parameters (D4, D16).

### 3.2 The per-namespace contract

Steps resolve everything by fixed name in the Workflow's own namespace. The component
creates all of it; defined once in `dark-factory-templates/values.yaml` → `contract`.

| Name | Kind | Purpose |
|---|---|---|
| `dark-factory` | ServiceAccount | Workflow identity: claims VMs, submits revision runs |
| `dark-factory-executor` | ServiceAccount | Token-less identity for steps running untrusted code |
| `dark-factory-coder` | SandboxWarmPool | The VMs `platform/code` claims |
| `dark-factory-github-orchestrator` | Secret (`token`) | Hub-side GitHub token. **Platform steps only** |
| `dark-factory-config` | ConfigMap, label `workflows.argoproj.io/configmap-type=Parameter` | Factory-wide settings (D11) |
| `dark-factory-holdout` | Secret, optional | Hidden scenarios, keys `<owner>-<repo>.json` (D17) |
| `dark-factory-step-secrets` | Secret, optional | Team step secrets, by declared key (§3.1) |
| `dark-factory-process` | WorkflowTemplate | The rendered process (D5) |

---

## 4. Decisions

### D1. The platform owns the merge decision; teams own their steps

The central decision. The case for reserving all step code to the platform is that step
code is a security control. Examine which steps that is true of:

| Step | Why platform-owned | Team-ownable? |
|---|---|---|
| `merge` | It *is* the merge decision: which statuses are required, and that a missing one is a refusal | **Never** |
| `approval` | Authorization: that a human approved, and from which team | **Never** |
| `holdout` | Confidentiality: the only reader of the hidden scenarios | **Never** |
| `security-scan` | Policy may require its verdict | **Never** |
| `changeset` | Feeds `skipWhen` and auto-merge; a wrong answer skips gates | **Never** (for now — §9 item 1) |
| Contract tests, licence scan, compliance tooling, model-graded rubric | Report a verdict. Block merge if the team put them in its process; cannot weaken anything else | **Yes** |


**Why team-authored steps are safe.** Three properties, each independently sufficient:

1. **No credentials.** No Kubernetes token, no IAM, no GitHub token, no holdout
   scenarios. A step cannot post a status, so it cannot forge one.
2. **No merge authority.** `platform/merge` requires the statuses the *platform* computed
   from the rendered process, plus policy's `requiredSteps`, which only platform steps can
   satisfy (D7). A team step can block its own team's merge; it cannot unblock anything.
3. **Isolated execution.** Kata micro-VM, default-deny egress, no control-plane reach
   (D4, D13).

**Costs.** The contract becomes a public ABI we must keep stable (D2). Step quality
becomes a team concern, so a flaky team step degrades that team's factory and our support
surface gets diffuse — status must make clear which step failed and who owns it (D20). And
a team can compose a process that passes policy while verifying very little (§5).

### D2. The step contract is a published ABI

| Option | Verdict |
|---|---|
| **Env in, result file out, same for all tiers** (**chosen**) | A team builds from documentation, in any language, with no Argo knowledge. The renderer needs no per-type knowledge; a new step is additive. |
| Argo parameters per step type | Teams would author `ClusterWorkflowTemplate`s — authoring in the cluster-scoped, platform-owned space. The one thing we must not hand over. |
| A step SDK or base image | Couples team steps to our release cadence, and to a language. |

Platform steps honor the same contract, with two differences only they may use:
credentials mounted by name, and posting their own status. Holding both tiers to one
contract keeps the registry homogeneous and lets a team step graduate to `shared/` without
a rewrite.

**Cost.** Once teams build against it, the ABI is expensive to change: a breaking change
is a *contract* version, not a step version, with both supported during the overlap (D9).

### D3. Step tiers and the registry

A step is addressed `<tier-or-team>/<name>`, resolved at render.

| Tier | Authored by | May hold credentials | May satisfy `requiredSteps` |
|---|---|---|---|
| `platform/<name>` | Platform | Yes | Yes |
| `<team>/<name>` | That team | No | No |
| `shared/<name>` *(§2)* | A team, promoted | No | No |

**The registry is a catalog, not a gate** — a ConfigMap of entries (name, digest, declared
capabilities, owner, docs) the platform publishes. A team is never blocked on being added:
`{use: acme/contract-tests, image: ghcr.io/acme/...@sha256:...}` works with no platform
involvement. The registry exists so a step becomes discoverable and reusable, which is the
part that benefits from being central.

Making the registry an allowlist instead would put the platform back in the critical path,
which is exactly what D1 removes; the security properties come from D1's three invariants,
not from registration. The one gate that stays is `allowedRegistries` (D7) — a
supply-chain control, not a curation decision.

### D4. Team steps: images, executed on the sandbox substrate

**Decision:** a digest-pinned OCI image, run by a platform-owned runner
(`df-step-runner-v1`) as a Kata pod in the factory namespace with
`automountServiceAccountToken: false`, the token-less executor identity, read-only root,
all capabilities dropped, and default-deny egress plus whatever `egress:` named.

| Option | Verdict |
|---|---|
| **Kata pod via the sandbox substrate** (**chosen**) | The isolation already exists for the coder: one substrate, one `ClusterNetworkPolicy`, one posture to reason about. |
| Hardened ordinary pod | Probably sufficient given no credentials, and faster to start — but a second posture to maintain, and on EKS VPC-CNI a namespaced NetworkPolicy does not reliably apply to Sandbox-owned pods (D13), so the policy story diverges too. Reasonable per environment; not the default. |
| Reuse the coder warm pool | The pool is built from the coder image; a step needs its own. |

**The runner is platform code**, so D1's invariants are enforced at every step launch
rather than by each team's image behaving well. It mounts the workspace, writes `DF_*`,
launches the image, reads `$DF_RESULT`, and posts the status under the step's reserved
context (D7).

**Costs.** Per-step Kata boot latency, paid in parallel across a stage but still paid.
And a team now owns an image lifecycle — build, scan, digest bump — real work we are
moving onto them in exchange for autonomy.

### D5. The process is rendered to a per-namespace WorkflowTemplate

| Option | Verdict |
|---|---|
| **The component renders `process` into `dark-factory-process`; tasks are `templateRef`s with `clusterScope: true`** (**chosen**) | KubeVela already renders and reconciles it, so a process change is a normal Application update. Readable, diffable in ArgoCD, holds no step code. |
| A cluster-scoped template interpreting the process at runtime | Nothing per factory, but `withParam` fan-out cannot express per-stage `onFail` or heterogeneous dependencies cleanly, debugging becomes "read the interpreter", and the UI shows a generic loop instead of named stages. |
| KubeVela's Application `workflow` field | Runs per deployment of the Application, not per issue. |

A stage renders as DAG tasks depending on the previous stage's; steps within a stage run in
parallel. Every team step renders as the same runner task with a different image and
config, so the rendered process is uniform regardless of tier.

**Costs.** A per-factory object holds the shape — acceptable because it holds no step code,
but a process bug can be unique to one factory. The renderer caps stages and steps per
stage (D7) rather than finding the etcd object limit in production.

### D6. Presets

**Decision:** `process.preset` names a platform-defined process the component expands
before applying overrides. `standard` is the Level 3 process the README describes —
implement, verify, approve, merge — and the recommended starting point. A team may add its
own steps without abandoning the preset:

```yaml
process:
  preset: standard
  approvals: 1
  maxIterations: 3
  addSteps:
    verify: [{use: acme/contract-tests}]
```

**Presets are not language profiles.** `PROFILES.md` rejected per-language `stackProfiles`
because the coder auto-detects toolchains from marker files. A preset describes a
*process* — who approves, what gates — which the platform must model and no repo
inspection can infer. The preset list must not grow language-shaped entries.

**Cost:** a process can come from a preset, `addSteps`, or explicit `stages`, so status
reports the **expanded** process, not the preset name (D20).

### D7. Guardrails: platform minimums at three enforcement points

```yaml
factoryPolicy:
  requiredSteps: [platform/security-scan]   # platform tier only, by construction
  minApprovals: 1
  allowedRegistries: ["ghcr.io/acme/", "<acct>.dkr.ecr.<region>.amazonaws.com/"]
  allowedEgressHosts: ["api.github.com", "*.acme-internal.com"]
  maxStages: 12
  maxStepsPerStage: 8
```

1. **Render.** A non-compliant process fails the Application naming the violated rule, so
   nothing deploys weakened. Covers `requiredSteps`, `minApprovals`, the allowlists, size
   caps, and that every step image is digest-pinned.
2. **Merge.** `platform/merge` requires the status of every step in the rendered process
   **and** every `requiredSteps` entry, declared or not — so deleting a required step does
   not produce a mergeable PR.
3. **GitHub.** Branch protection requiring the `dark-factory/*` checks.

Each covers the others' bypass: render gives the good error message but is only reached
through the component; merge catches a hand-written Workflow that skipped a step; branch
protection catches one that skipped merge.

**Two rules exist specifically because teams author steps:**

- **`requiredSteps` may only name `platform/` steps**, or a team satisfies a
  platform-mandated security gate with its own stub. The most important guardrail here, and
  structural rather than configurable.
- **Status contexts are namespaced by tier.** Platform steps own `dark-factory/<name>`,
  team steps `dark-factory/team/<step-name>`. The runner — not the step — chooses the
  context, so the reservation is enforced, not requested.

Trust follows the tier rather than a prefix allowlist, which would conflate "which
external checks may `ci-check` wait on" with "which checks are trusted".

**Cost.** Policy lives in Helm values, invisible in the tenant's Application, and is
per environment — so one Application can be valid on one cluster and rejected on another.
Intended for dev versus production, but it makes promotion a thing to test.

### D8. Failure handling, fast paths, and one iteration budget

| `onFail` | Behaviour |
|---|---|
| `stop` (default) | The run fails; statuses stay red; the PR is not mergeable |
| `revise` | Failing steps' `summary` and `details` go back to the coder, consuming one iteration |
| `notify` | Post to the PR and continue. For advisory checks |

`revise` is where team steps earn their place: a team's own contract-test failure feeds the
coder exactly as the holdout gate does, because both return the same contract (D2).

**One budget for all revisions.** `onFail: revise` and human PR comments consume the same
`maxIterations`, counted per PR from the revision rounds recorded on the PR, not two
independent counters — otherwise `revise` on three stages plus comment-driven iteration has
no effective cap, the one control §5 most needs. A round that would exceed it posts a
"human needed" comment and stops, naming the stages that were fighting.

**Fast paths.** `skipWhen: {pathsOnly: [...]}` on a stage, the one genuinely runtime
condition: `platform/changeset` runs early and emits changed paths that stage `when:`
expressions read. The condition is "paths are a subset of", never "paths include", so a PR
touching `docs/` and one line of `src/` does not match.

**Concurrency.** `maxConcurrentRuns` is an Argo semaphore in the namespace; the per-issue
mutex is keyed on the issue number (D21) so a revision run and its original serialize
instead of each claiming a VM.

### D9. Versioning, and who owns a version

| Tier | Versioned by | Pinned by |
|---|---|---|
| `platform/<name>` | Platform, as `df-step-<name>-<version>` pinning an image digest | `catalogVersion`, or per step |
| `<team>/<name>` | The team, as an image digest | The digest in the Application |

A factory pins `catalogVersion` and the component renders every platform `templateRef` at
it, so one run never mixes platform versions. A compatible fix keeps its version; an
incompatible change (new required `config` key, changed status semantics) ships as a new
version of **that step**. Team steps are pinned by digest, never a tag — enforced at render
(D7). The contract itself is versioned separately (D2).

**Cost:** three things carry versions (contract, platform steps, team images). Status
reports the resolved set (D20).

### D10. Platform steps are images too

Platform steps run logic adapted from the `dark-factory` chart (`merge.js`, `iterate.js`,
`security-wait.js`, `evaluate.js`, `holdout-gate.sh`), which that chart mounts from a
ConfigMap — and a ConfigMap cannot cross namespaces.

| Option | Verdict |
|---|---|
| **One image per platform step, digest-pinned in its template** (**chosen**) | Same packaging as a team step, so there is one execution model, one contract, one way to test. |
| Scripts embedded in the templates as Argo raw artifacts | No image build, but it makes platform and team steps structurally different things — see below. |
| One image carrying every platform step's scripts | Fewer artifacts, but a change to `merge` re-releases `holdout`, and per-step versioning (D9) stops meaning anything. |

**Why images, when embedding scripts would avoid a build.** Because the two tiers being
the *same shape* is worth more than the build it saves:

- **It makes the reserved set movable.** §9 item 1 wants `changeset` and holdout's
  evaluator to become team-ownable. If platform steps are images honoring the contract,
  that is a registry entry change. If they are scripts fused into cluster-scoped
  templates, it is a rewrite — and the reduction stays theoretical.
- **D2's graduation claim becomes true.** A team step promoted to `shared/`, or a platform
  step handed to a team, is the same artifact either way.
- **One test path.** §7's conformance suite covers platform steps unmodified, instead of
  platform steps needing their own harness.
- **It drops a pile of incidental mechanism**: no `.Files.Get`, no raw artifacts, no
  symlinking the chart's `scripts/` *and* `holdout/evaluate.js` into the templates chart,
  no confirming ArgoCD follows those symlinks, and no rewriting `jq` in node to suit a
  stock base image. The image installs what it needs.

**Drift is handled by the digest, not by fusion.** The worry that motivated embedding —
template `v1` silently running a different `merge.js` — is a *mutable tag* problem. Each
step template pins its image **by digest**, exactly as team steps are pinned (D7), so
`df-step-merge-v1` resolves to one immutable artifact. Tenants still cannot change it: the
digest lives in a platform-owned, cluster-scoped template.

**What platform steps still do that team steps cannot** is unchanged and declared in the
template, not the image: mount credentials by name, and post their own GitHub status. That
is the whole remaining difference between the tiers.

**Publishing is an operational choice, not part of this design.** All the design requires is
that a template pins an immutable digest. The recommendation is to build once into a
platform account and **mirror** to each hub — ECR replication copies the manifest and so
preserves the digest; rebuilding per account gives the same source a different digest and
leaves nothing meaningful to pin. (ECR pull-through cache only fronts public upstreams, so
it is not the mechanism here.)

**Costs.** A build-and-publish pipeline we did not have, and several small images instead of
one.

### D11. Factory-wide settings in a ConfigMap

Parameters default from `dark-factory-config` via `valueFrom.configMapKeyRef`, which Argo
resolves in the **Workflow's** namespace, so one template serves every factory. Keys are
only genuinely factory-wide: `coder-engine`, `bifrost-url`, `claim-ttl-seconds`,
`max-iterations`, `max-concurrent-runs`, `run-timeout-seconds`. Per-step tuning travels in
`DF_CONFIG`. The alternative — the Sensor injecting every setting as an argument — makes
every trigger carry every setting.

Settings are read when a run starts, so a change applies to the next run. The ConfigMap
needs the label `workflows.argoproj.io/configmap-type: Parameter` or Argo ignores it;
verified on `dora-showcase` that `argo-cluster-role` grants the controller
`configmaps get/list/watch`. A step a team does not want is simply absent from its process,
so there are no `*-enabled` keys and no skipped nodes in the UI.

### D12. Fixed contract names; one factory per namespace

Fixed names keep templates readable; deriving them from a `factory` parameter would make
every name an expression, and some fields (template-level executor SA, ConfigMap refs used
to *resolve* parameters) cannot be safely templated. The namespace is Kubernetes' isolation
boundary for RBAC, Secrets, NetworkPolicy and quotas — two factories sharing one would
share credentials, blast radius, and now two teams' step images in one isolation domain.

The workflow ServiceAccount is `dark-factory`, not `context.name` as the OAM steering doc
prescribes: it must never gain an AWS identity (D16), so not being a trait anchor is a
feature.

### D13. Control-plane isolation keyed on a pod label

On EKS VPC-CNI an ordinary NetworkPolicy does not reliably apply to Sandbox-owned pods, so
the real isolation is an Admin-tier `ClusterNetworkPolicy` selecting
`agent-sandbox.io/role in (coder, step)` in any namespace — covering every factory's coder
and every team step automatically. The runner stamps `role: step` on each step pod, so
coverage does not depend on the image cooperating. A per-factory policy would mean a tenant
Application owning cluster-wide admin-tier network policy.

The component also keeps a namespaced default-deny egress policy as defence in depth, plus
a per-step policy from its declared `egress:` hosts.

**The two Admin policies must not share a priority.** Both default to `adminPriority: 10`,
and if the controller requires uniqueness per tier one is rejected — the label-keyed one
being the one that matters for non-hub factories. The second takes `adminPriority + 1`, to
be confirmed on a live VPC-CNI.

### D14. Event plumbing per namespace

EventBus, EventSource, Sensor and Ingress in each factory namespace, so each receives only
its own repos' webhooks under its own HMAC secret and a broken factory cannot affect
another. One central EventSource routing to namespaces would be fewer components but a
single service holding every factory's webhook secrets — one point of failure and of
compromise.

**Cost:** a JetStream EventBus per namespace (3 small pods), shareable with
`eventBus.create: false`. Webhook paths include the namespace
(`/dark-factory/<ns>/<name>`), so factories share one ALB host without colliding.

### D15. Three tokens per factory, from Secrets Manager

ExternalSecrets from the `aws-secrets-manager` ClusterSecretStore at
`<cluster>/dark-factory/<namespace>/<name>/github/{coder,orchestrator,events}`:

| Token | Used by | Scope |
|---|---|---|
| coder | The coder VM (0400, never in env) | Issues read; contents, PRs, statuses write. **No** merge path, no Issues write |
| orchestrator | **Platform steps only** | Adds Issues write and merge |
| events | EventSource only | Webhook registration and HMAC |

Team steps get none of these (D1); one needing its own credential declares `secrets:` and
gets keys from the factory's `stepSecrets/` prefix, which the team controls and which never
holds a GitHub token we issued. The most exposed components hold the least privilege, and
the cluster prefix lets each cluster's External Secrets identity be limited to its own
paths.

### D16. No AWS identity anywhere in a factory

No factory ServiceAccount carries IAM, and `factoryPolicy` forbids attaching an identity
trait to one. `platform/security-scan` waits for the Security Agent **GitHub App** and
mirrors its verdict rather than calling AWS APIs; the chart's IRSA-plus-S3 mode is not
ported. The coder and every team step execute untrusted code in a shared namespace, so
having no AWS credentials there at all is simpler and stronger than scoping them per step.

**Cost:** `security-scan` needs the App installed per repo, and a future `deploy` step is
harder than it would be with IRSA. Intended — a team step needing cloud access gets a
scoped credential through `stepSecrets`, not an identity in the namespace.

### D17. Holdout scenarios: a synced Secret

Scenarios live in `dark-factory-holdout`, one key per repo (`<owner>-<repo>.json`), synced
from the secrets store like the GitHub tokens; `platform/holdout` mounts it read-only and
optional. They are the one piece of genuinely confidential tenant data in the factory, so
the secrets store is where they belong — a hand-applied ConfigMap would sit outside GitOps
and outside status. They stay out of the Application, because an Application living in the
target repo would let the coder read them, and **no team step can request them** — there is
no capability that grants them, which is why holdout cannot be team-ownable (D1). A repo
with no key fails closed (`NOT VERIFIED`) while `requireScenarios` is true, the default.

**Cost:** `platform/holdout` runs coder output with no Kubernetes token, but still holds
the orchestrator token to post its status — which is why sandboxing it is a follow-up (§8).

### D18. `ci-check`: wiring checks that already run elsewhere

`platform/ci-check` waits (bounded) for a named GitHub check and declares it so merge
requires it — for checks a team already runs outside the factory:

```yaml
- {use: platform/ci-check, config: {check: "ci/unit-tests"}}
```

It is **not** the path for bespoke tooling; that is a team step (D1), which can see the
workspace, feed `onFail: revise`, and run inside the factory. A timeout is a failure, hence
a refusal at merge.

### D19. Platform-supplied environment values

Developers never set account, region, cluster name, webhook host, or Bifrost's address.
These flow from the cluster secret's annotations through the addon registry to Helm globals
(`awsAccountId`, `awsRegion`, `clusterName`, `bifrostUrl`, `bifrostClusterIp`,
`ingressDomain`) and into CUE defaults. Bifrost is reached by **ClusterIP** (Kata guests
cannot resolve cluster DNS) and admitted by a `/32` egress rule, because VPC-CNI matches
egress on the pre-DNAT ClusterIP. `webhook.host` defaults to
`<name>.<namespace>.<ingressDomain>`.

### D20. Status, and prerequisites the platform owns

An Application that claims to describe the factory, beside a factory that silently fails
because a secret is missing, is an abstraction leak. With team-authored steps status
carries more weight still: it is where a team sees that its own step is breaking its
factory.

`customStatus` and `healthPolicy` report the **expanded** process (stages, steps, each
step's tier, resolved platform versions and team digests), each prerequisite's state (the
three tokens, the holdout source per repo, warm pool readiness, webhook registration, step
images resolvable), and which `factoryPolicy` applied. The platform also takes over
namespace creation, `webhook.host` defaulting (D19), and the holdout source as a
secrets-store reference (D17); the team keeps what lives in GitHub — the Apps, branch
protection — and its own step images.

**The current `healthPolicy` must be replaced, not extended:** copied from a
Deployment-shaped component, it reads
`context.output.status.readyReplicas >= context.output.spec.replicas` against a
`SandboxWarmPool`, which has no such fields, so it can only ever report the `| false`
fallback.

**Cost:** status needs state from objects the component does not own, so `customStatus`
expressions get long and brittle. A reconciler would be better (§8).

### D21. Smaller choices

| Choice | Reason |
|---|---|
| Run key is the issue **number**, not the issue id | The number is unique per repo and in every payload; `issue.id` is not — a revision run resolved from a PR has only the number. Keying on the id would give a run and its revision different mutexes and claim names, so both could hold a VM for one issue. |
| `synchronization.mutexes` (list) | Argo v4 removed `mutex`. Verified: v4.1.4 rejects it; the list form works on v3.6+. |
| No issue title in the claim | Raw issue text broke the claim manifest on quotes. The coder reads the title from the issue it fetches. |
| Revision notes base64-encoded | Arbitrary markdown cannot break the claim manifest. |
| Deterministic Workflow names per event | GitHub redelivers webhooks; duplicates collide (`AlreadyExists`) instead of racing. |
| Registry gate `enable_dark_factory` | One label already turns on the Dark Factory stack; the step templates join it. |

---

## 5. Security model

| Threat | Control |
|---|---|
| Coder code reaches credentials | Kata VM (own kernel); no Kubernetes token, no IAM; only a narrow GitHub token at `/etc/secrets` (0400) |
| **Team step reaches credentials** | **No tokens mounted (D1, D15); only declared `stepSecrets` keys, which the team owns** |
| Coder or team step reaches the control plane | Admin-tier ClusterNetworkPolicy on `role in (coder, step)` (D13); default-deny egress plus declared hosts |
| **Team step forges another step's verdict** | **Steps never post statuses; the platform runner reads `$DF_RESULT` and posts under a tier-namespaced context it chooses (D2, D7)** |
| **Team step satisfies a required gate with a stub** | **`requiredSteps` may only name `platform/` steps — structural, not configurable (D7)** |
| **Hostile step image** | **Digest-pinned from an allowed registry (D7); Kata pod, no tokens, read-only root, caps dropped, deny-all egress (D4). Worst case: that team's own PRs fail to merge** |
| Coder games its own tests | Hidden scenarios no step but `platform/holdout` can reach (D17); a judge from a different model family; fail-closed on missing scenarios |
| Coder output executed on the hub (holdout) | `npm install --ignore-scripts`; no Kubernetes token; no AWS identity in the namespace (D16) |
| Unreviewed or failing code merged | Merge only on a human approval event; `platform/merge` refuses unless every required status is green at the PR head; a missing status is a refusal |
| Tampered platform step code | Each platform step's image is digest-pinned in a cluster-scoped, platform-owned template (D10); only the platform changes the digest, and every change ships as a step version |
| Tenant process drops a required gate | Three overlapping enforcement points (D7) |
| Runaway revision loop | One iteration budget shared by `onFail: revise` and comments (D8); factory-authored comments filtered at the Sensor |
| Two runs for one issue holding two VMs | Mutex and claim keyed on the issue number (D21); `maxConcurrentRuns` semaphore (D8) |
| Cross-factory interference | One factory per namespace (D12); per-namespace events and secrets (D14, D15); Argo locks are namespace-scoped |

**Residual risks**

- Branch protection is the only hard stop on self-merge, and is unavailable on private
  repos on GitHub's free plan.
- **Render-time policy binds only the component path.** A namespace admin can still submit
  a raw Workflow calling platform steps directly, or none at all.
  `templateReferencing: Strict` would close that cluster-wide (§8).
- **A valid process can still be a bad process**, and team steps widen this: a team can
  compose something that passes policy and verifies very little, or whose own steps always
  return success. Policy floors *our* gates, not the quality of a team's.
- **A team step can exfiltrate the source it is given.** With `workspace: true` and any
  `egress:` host, a step sees the branch and can post it outward. Inherent in letting teams
  run code against their own source, which is why `allowedEgressHosts` is per-environment
  policy rather than a team setting.
- The coder's token is a standing credential; short-lived per-run App tokens would be
  stronger.
- `platform/holdout` holds the orchestrator token while running coder output — the last
  platform step running untrusted code with a credential mounted (§8).

---

## 6. Operations

**Platform, once per hub.** Make the platform step images pullable in this hub's account,
so the digests their templates pin resolve (D10). Set `enable_dark_factory=true`, which
syncs `dark-factory-templates` plus the agent-sandbox stack. Set `factoryPolicy` for the
environment (D7) — the shipped default is production-shaped: `platform/security-scan`
required, one approval, no registries allowed until the operator names them. Enable
`enable_oam_components`, then regenerate the component YAML with `platform/oam/generate.sh`
and **commit the result**: `oam-agent-components/templates/dark-factory.yaml` is a
generated, checked-in file like the other components', and the chart cannot register the
type without it.

**Team, per factory.** Apply the Application, starting from `preset: standard`. Install the
Security and DevOps Agent GitHub Apps on each repo (or leave those steps out, if policy
allows) and enable branch protection requiring the `dark-factory/*` checks. Seed the
holdout scenarios and the three tokens at the paths status reports as missing.

**Team, per step it owns.** Build an image whose entrypoint honors §3.1 — any language, no
SDK. Push it by digest to a registry in `allowedRegistries`. Reference it in `process` with
the capabilities it needs, and bump the digest to release.

**Changing the process.** Edit `process`; KubeVela re-renders and the next run uses it.
In-flight runs finish on the old shape. A violation of `factoryPolicy` fails at render.

**Upgrading platform steps.** A compatible fix keeps its version; an incompatible change
ships at a new one, and factories move by bumping `catalogVersion` or that step's `version`,
with the old template kept until every factory has moved. Team step upgrades are the team's
own digest bump (D9).

---

## 7. Validation

Already established, on the substrate rather than this design's shape: `mutexes` accepted
and `mutex` rejected on Argo v4.1.4; a server-side dry run on `dora-showcase` for the
component's namespaced objects (Sandbox and ExternalSecret CRDs are absent there); and the
holdout evaluator against sample scenarios with a stub judge that always says yes — an
honest implementation passed 4/4, a hard-coded one failed 2/4, to be re-run in the
`platform/holdout` image (D10).

Needed, none of it done:

- **A conformance suite for the step contract** — a reference step in two languages, plus
  adversarial ones: no result file, malformed JSON, non-zero exit, a result claiming
  `success` after an internal error, a step attempting to post a GitHub status, a step
  reaching for the API server and the holdout Secret. The last three must fail closed. This
  suite is the executable form of D1's three invariants.
- CUE type-checks: each preset, `addSteps`, an explicit process, a violation of each
  `factoryPolicy` rule (must fail naming the rule), a tag-not-digest image, and a
  `requiredSteps` entry naming a team step (must be rejected).
- A rendering golden test per preset: stage order, step set, and the required-check list
  `platform/merge` receives.
- Per-platform-step tests against the contract, each starting with empty `DF_PR_NUMBER`.
- `helm lint` and `argo lint` on every platform step template and the runner; every
  `configMapKeyRef` key present in the rendered `dark-factory-config`.
- Live confirmation that `configMapKeyRef` defaults resolve for a Workflow submitted against
  a namespaced `WorkflowTemplate` whose tasks are `templateRef` with `clusterScope: true` —
  documented by Argo and allowed by the controller's RBAC, but unobserved and now
  load-bearing at two levels.
- Both Admin ClusterNetworkPolicies admitted with distinct priorities on a live VPC-CNI, and
  that `role=step` covers runner-launched Kata pods (D13).
- Kata cold-boot latency for a trivial step image, to decide whether §2's "no warm pool"
  holds.
- A live run on `preset: standard` (label → VM → PR → steps → approval → merge), then one
  with a team step and `onFail: revise`.

---

## 8. Follow-ups

1. **The `shared/` registry tier** — promotion, review and ownership for a team step others
   depend on.
2. **Cosign verification of step images**, alongside `allowedRegistries`.
3. **A warm pool for step execution**, if Kata boot latency proves to matter.
4. **Sandbox `platform/holdout`** — the last platform step running untrusted code with a
   credential mounted. The runner (D4) is most of the mechanism; holdout needs the
   orchestrator token removed and its status posted for it.
5. **Move the chart's hub factory onto the step templates**, removing the last duplicated
   pipeline. **Port the chart's templates to `mutexes`** — they fail on Argo v4 today.
6. **Evaluate `templateReferencing: Strict`** on the hub's Argo controller — the only thing
   that would close the inline-Workflow bypass.
7. **Short-lived GitHub App tokens** per run instead of standing tokens.
8. **Update `setup-secrets.sh`** for per-factory paths, the holdout source and `stepSecrets`.
9. **Implement D10:** split the chart's scripts into one image per platform step, each
   honoring the contract; pin each by digest in its template; stand up the build-and-publish
   pipeline; delete `examples/dark-factory/steps/` and `stepsImage`.
10. **A reconciler for status** instead of long `customStatus` expressions (D20).

---

## 9. Open questions

1. **Reserve platform templates for security controls only.** The direction D1 implies:
   keep shrinking the platform-owned set until a step is reserved *only* because a team
   implementation could weaken a security control, never because it could merely be wrong.
   `merge`, `security-scan` and `approval` are the boundary and stay. `changeset` can move
   once the platform evaluates skip decisions from a team-supplied path list rather than
   trusting a team step's boolean — then a wrong changeset is a wrong input, not a skipped
   gate. `holdout` splits: the scenario mount stays platform-only, but the evaluator could
   be a team step given scenarios it cannot exfiltrate (§8 item 4). `platform/code` is not
   reserved at all today, so a team wanting its own coder or prompt strategy should already
   be able to ship one — untested. The end state is a platform owning the merge decision,
   the isolation substrate and the credential boundary, and nothing else; getting there is
   mostly moving trust decisions out of step *implementations* and into platform-evaluated
   *data*, the same move D7's `requiredSteps` rule already makes.
2. **How does a team test a step, or a process change,** without labelling a real issue on a
   real repo? A local runner feeding a step image the `DF_*` environment and checking its
   result covers the step case cheaply and is probably the highest-value thing to ship
   alongside the ABI — and since D10, it tests platform steps too. The process case has no
   answer yet.
3. **How is `platform/approval` verified?** GitHub review state is the obvious signal, but
   "two approvals from `acme/security`" needs team-membership lookups and CODEOWNERS is
   enforced by branch protection rather than visible to the step — so the two can disagree.
4. **May a team step request `egress` to the LLM gateway?** A model-graded check is an
   obvious team step and Bifrost is already reachable from the coder, but allowing it means
   team steps consume gateway quota under the factory's identity.
5. **Who owns a team step that outlives its team?** The registry records an owner, but a
   factory pinned to an abandoned digest keeps working with nobody to fix it.
6. **Is `maxConcurrentRuns` per factory or per repo?** Per factory as specified; a factory
   watching ten repos may want per-repo limits.
7. **What do consumers outside our accounts pull?** This repo is public, so the digests in
   the shipped templates need either a public upstream to mirror from or to be treated as
   placeholders an operator re-pins.

---

## Appendix: files

| Path | Role |
|---|---|
| `platform/oam/definitions/components/dark-factory.cue` | The component: per-namespace resources, `process` schema, step resolution, preset expansion, policy validation, rendered process |
| `gitops/addons/charts/oam-agent-components/templates/dark-factory.yaml` | Generated by `platform/oam/generate.sh`. **Checked in**; the chart cannot register the type without it |
| `docs/dark-factory/STEP-CONTRACT.md` | The published ABI (§3.1), versioned independently — what a team builds against |
| `gitops/addons/charts/dark-factory-templates/` | Platform step templates (each pinning its image digest), the step runner, the registry ConfigMap, the contract, `factoryPolicy` |
| `examples/dark-factory/platform-steps/` | One image per platform step, built from the chart's scripts (D10) |
| `examples/dark-factory/team-step/` | Reference team step image and its conformance test — what a team copies to start |
| `platform/oam/examples/example-dark-factory.yaml` | Example: preset form, plus a team step |
| `gitops/addons/registry/sandbox.yaml` → `dark-factory-templates` | Registers the chart and the step-image registry prefix for this hub |
| `examples/dark-factory/steps/` | Monolithic steps image and `holdout-gate.sh`. **Superseded by D10's per-step images** |
| `gitops/addons/charts/agent-sandbox/templates/31-clusternetworkpolicy.yaml` | Admin-tier policy keyed on `role in (coder, step)`, distinct priority (D13) |
| `gitops/addons/charts/dark-factory/scripts/`, `holdout/evaluate.js` | Source the platform step images are built from, until the hub factory moves onto them (§8 item 5) |
| `docs/dark-factory/PROFILES.md` | Why there are no language profiles, and why process presets differ (D6) |
