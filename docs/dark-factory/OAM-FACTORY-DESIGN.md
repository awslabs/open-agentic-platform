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

That leaves the central claim: **we already execute untrusted code safely** — an agent
sandbox with its own kernel, no Kubernetes token, no IAM, default-deny egress. A team's step image on that
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
guardrails; and the per-namespace factory — identities, four short-lived tokens minted from the platform GitHub App, warm pool,
holdout source, settings, webhook trigger, status. The contract is **substrate-neutral**:
a factory runs on whichever sandbox runtime the **cluster** is configured for — Nitro by
default, Kata by annotation, as `type: sandbox` does (#151) — and never chooses one itself.
Lambda MicroVM is sequencing, not a non-goal (§3.2).

**Not building**

| | Why / instead |
|---|---|
| The `shared/` registry tier | Teams reference their own steps directly; promotion needs a review workflow and an owner (D3) |
| Signature verification of step images | Digest pinning and an allowed-registry list ship now; cosign is a follow-up |
| A warm pool for step execution | Steps run as agent sandboxes on demand; pool once real durations are known (D4) |
| `deploy`, environment promotion | Nothing needs it before merge gating works |
| Pluggable intake | `issue-labeled` with a configurable label; issue types and slash commands are additive |
| Autonomy 3.5 (low-risk auto-merge) | Targets the README's Level 3; needs `changeset` proven first |
| Per-host step egress | EKS enforces `domainNames` rules only on Auto Mode nodes, and Kata pools are self-managed; v1 step egress is public-or-none, with per-host egress an option once §7 confirms it for Nitro guests (D13) |
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
      │   claim SandboxWarmPool dark-factory-coder ─► sandbox (coder)       │
      │   mount Secret    dark-factory-holdout  (hidden scenarios)         │
      │   TEAM STEPS ─► sandbox, no tokens, egress only if granted         │
      └────────────────────────────────────────────────────────────────────┘
```

### 3.1 The step contract

A **published ABI**, not an internal convention: teams build against it without reading
our templates. A step is an OCI image whose entrypoint reads the environment and writes
one result file.

| Env in | |
|---|---|
| `DF_REPO` | `owner/name` |
| `DF_ISSUE_NUMBER` | The originating issue. With `DF_REPO`, forms the run key (D21) |
| `DF_PR_NUMBER` | Empty before `platform/code` opens the PR |
| `DF_BRANCH`, `DF_BASE_BRANCH` | `df/issue-<n>`, and the PR base |
| `DF_STEP_NAME` | This instance's name; namespaces its status context |
| `DF_CONFIG` | JSON object of this step's `config` from the Application |
| `DF_WORKSPACE` | The checked-out branch, if the step requested it |
| `DF_RESULT` | Path the step must write its verdict to |

Out — two channels, kept apart:

- **The verdict**: one small JSON file at `$DF_RESULT`, and nothing else in it:

  ```json
  {"status": "success|failure|skipped", "summary": "one line, at most 256 characters"}
  ```

- **The output**: whatever the step prints to stdout and stderr. The runner collects the
  logs, links them from the check-run, and on `onFail: revise` sends the coder the `summary`
  plus the log's tail (D8). There is no size limit and no format; a team prints what it wants
  a reader to see.

The verdict travels as the pod's termination message (D4), whose 4 KB limit a two-field
verdict never approaches. A non-zero exit with no result file is a `failure` carrying the
last log line, and a result the runner cannot parse — malformed, or truncated because a step
wrote more than the limit (the kubelet keeps the *last* 4096 bytes, so `status` would be
lost) — is a `failure`, never a partial pass.

A step **never posts its own verdict**: the runner reads the result and the platform posts
it as a **check-run from the platform GitHub App** (D15). What makes a check a verdict is
**which App created it**, not its name:

- `platform/merge` accepts a required `dark-factory/*` check only if its `app.id` is the
  platform App's. A same-named commit status, or a check-run from any other App, counts as
  missing, which means a refusal.
- The branch ruleset requires each `dark-factory/*` check **from the platform App**
  (rulesets can pin a required check to an App), so GitHub enforces the same rule when a
  human merges in the UI.
- Only the orchestrator token carries `checks: write`. The coder, reader and events tokens
  are minted from the same App without it, and a `stepSecrets` token cannot act as the App,
  so nothing else in a factory can create a platform check.

**Capabilities**, requested in the Application and granted narrowly: `workspace: true`
(the branch at `$DF_WORKSPACE`, on a per-launch volume deleted with the run, D4), `egress: true` (public HTTPS,
port 443 to any address outside the cluster's and the VPC's private ranges; granted only
where `factoryPolicy.stepEgress` is `public`, D7; without it a step gets DNS only),
`secrets: [<name>...]` (keys from the factory's
`stepSecrets/` prefix, read-only).

Nothing else is reachable. There is no capability granting the orchestrator token, the
holdout scenarios, a Kubernetes token, AWS credentials, the LLM gateway (Bifrost), or merge
authority — those are absences, not parameters (D4, D16). A model-graded team step brings its
own model credential through `secrets:` and calls a public endpoint with `egress: true`, so
team steps never consume gateway quota under the factory's identity.

### 3.2 The per-namespace contract

Steps resolve everything by fixed name in the Workflow's own namespace. The component
creates all of it; defined once in `dark-factory-templates/values.yaml` → `contract`.

| Name | Kind | Purpose |
|---|---|---|
| `dark-factory` | ServiceAccount | Workflow identity: claims VMs, submits revision runs |
| `dark-factory-executor` | ServiceAccount | Token-less identity for steps running untrusted code |
| `dark-factory-coder` | SandboxWarmPool + SandboxTemplate `dark-factory-coder-template` | The sandboxes `platform/code` claims, rendered through #151's sandbox contract |
| `dark-factory-coder-sandbox` | ConfigMap (card) | Pool name, `idleTimeout` and `maxLifetime`, published as `type: sandbox` publishes them; the claimer reads its TTLs here |
| `dark-factory-github-orchestrator` | Secret (`token`) | Hub-side GitHub token. **Platform steps only** |
| `dark-factory-config` | ConfigMap, label `workflows.argoproj.io/configmap-type=Parameter` | Factory-wide settings (D11) |
| `dark-factory-holdout` | Secret, optional | Hidden scenarios, keys `<owner>-<repo>.json` (D17) |
| `dark-factory-step-secrets` | Secret, optional | Team step secrets, by declared key (§3.1) |
| `dark-factory-process` | WorkflowTemplate | The rendered process (D5) |

**Runtime.** The process depends only on the Sandbox CRD surface, and the **cluster**, not
the factory, decides which runtime backs it. The component follows #151 (`type: sandbox`)
exactly, by reusing its CUE locals and guards rather than composing the component:

- **Class.** `runtimeClassName` comes from the cluster secret's `sandbox_runtime_class`
  (with `confidential_sandbox_runtime_class` and `nitro_sandbox_runtime_classes`), as
  `sandbox.cue` reads it — Nitro by default, Kata when the cluster sets `sandbox_runtime: kata`.
  No Application property reaches it, and there is no `substrate` key. It is the **only**
  scheduling field: no nodeSelector or toleration is stamped, because RuntimeClass admission
  merges them (a Kata nodeSelector on a Nitro class leaves every pod Pending). Where a label
  naming the substrate is useful, it is derived from the class.
- **Coder pool.** `dark-factory-coder` is rendered with #151's contract: the SandboxWarmPool
  is the workload (so health is real, D20), the SandboxTemplate, the ServiceAccount and the
  card; `sandbox.amazonaws.com/*` annotations go on the pod template, never the template's own
  metadata. The template sets `envVarsInjectionPolicy` explicitly and
  `networkPolicyManagement: Unmanaged`, so the operator does not contend with the chart for
  one NetworkPolicy (#151 §9.4); isolation comes from D13's ClusterNetworkPolicy.
- **Trust model.** The coder is `mode: isolated`: the coder flow needs `exec`, which
  `confidential` denies. v1 offers `confidential` to no step, because per-run inputs arrive as
  env and a confidential Cedar policy pins env exactly.
- **Guards.** Render fails, as in #151, when no class is configured; when `networkIsolated`,
  `cedarPolicy` or confidential lands on a non-Nitro class; when confidential lacks a
  `cedarPolicy` or is combined with `networkIsolated`; when `networkIsolated` is combined
  with a service; or when a policy or digest is still an example placeholder. The same guards
  apply to the coder pool and to runner-launched steps (D4), with rows in #151's
  `scripts/test-isolation-matrix.sh`.
- **Role label.** The factory stamps `agent-sandbox.io/role: coder` on the coder and `step`
  on runner-launched steps, not `type: sandbox`'s `session`; without it a pod silently leaves
  D13's isolation. Reusing #151's CUE rather than composing `type: sandbox` is what lets the
  factory set the role without a change to #151.

Claim and release stay the one runtime-specific part (Lambda needs suspend and resume); they
belong in a per-runtime step that `platform/code` calls, so a new runtime does not fork
platform step versions (D9).

### 3.3 Platform steps

Each platform step honors the §3.1 contract and posts its verdict under
`dark-factory/<name>` (D7). The decision named in each row covers the step in depth.

| Step | Does | Config | Posts |
|---|---|---|---|
| `platform/code` | Claims a coder sandbox, runs the coder on the issue (or on revision notes), pushes `df/issue-<n>`, opens or updates the PR, releases the sandbox | `engine` (default from `coder-engine`) | `dark-factory/code` |
| `platform/changeset` | Lists the PR's changed paths so later stages can evaluate `skipWhen` (D8) | none | `dark-factory/changeset` |
| `platform/ci-check` | Waits, with a time limit, for a named external GitHub check and copies its verdict; a timeout is a failure (D18) | `check`, `timeoutSeconds` | `dark-factory/ci-check-<check>` |
| `platform/holdout` | Runs the hidden scenarios against the branch through the runner, then has a judge confirm them; fails closed if the repo has no scenarios (D17) | `threshold`, `requireScenarios`, `judgeModel`, `judgeRuns`, `judgeQuorum` | `dark-factory/holdout` |
| `platform/security-scan` | Waits for the Security Agent App's review and copies its verdict, failing at or above `blockLevel` (D16) | `blockLevel`, `waitSeconds` | `dark-factory/security-scan` |
| `platform/approval` | Ends the run until a review arrives, then starts the continuation run; passes when the PR's GitHub `reviewDecision` is `APPROVED` at the head commit (D8, D20) | none | `dark-factory/approval` |
| `platform/merge` | Re-reads the PR head, requires a platform-App check for every rendered step and every `requiredSteps` entry, merges, closes the issue (D7) | none | none (merges) |

### 3.4 Component properties

| Property | Type | Default | Config key |
|---|---|---|---|
| `repositories` | `[{owner, names}]` | required | — |
| `triggerLabel` | string | `dark-factory` | — |
| `catalogVersion` | string | `v1` | — (D9) |
| `process.preset` | string | `standard` | — (D6) |
| `process.stages` | `[{name, steps, onFail, skipWhen}]` | from preset | — |
| `process.addSteps` | map of stage → steps | `{}` | — |
| `process.approvals` | int | `1` | — (D7) |
| `process.maxIterations` | int | `3` | `max-iterations` |
| `maxConcurrentRuns` | int | `2` | `max-concurrent-runs` |
| `coder.engine` | `claude` \| `kiro` | `claude` | `coder-engine` |
| `coder.image` | string | platform ECR | — |
| `coder.warmPoolSize` | int | `0` | — (see below) |
| `coder.runTimeoutMinutes` | int | `30` | `run-timeout-seconds` |
| `coder.idleTimeout`, `coder.maxLifetime` | duration | #151's defaults | — (published on the card, §3.2) |
| `coder.limits` | object | #151's defaults | — (Nitro's DNS-packet and outbound-connection throttles, passed through as `type: sandbox` exposes them) |
| `webhook.host` | string | `ingressDomain` | — (D19) |
| `secrets.keyPrefix` | string | `<cluster>/dark-factory/<ns>/<name>` | — (D15) |
| `holdout.requireScenarios` | bool | `true` | — (D17) |
| `eventBus.create` | bool | `true` | — (D14) |

A step's own settings go in its `config` and reach it as `DF_CONFIG` (§3.1).
Platform-supplied values (account, region, Bifrost, the runtime class) are not properties
(D19, §3.2). GitHub App installation IDs are not properties either: they live in the
platform's onboarding record (D15).

**Why `warmPoolSize` defaults to 0.** The shipped claim injects env
(`20-workflowtemplate-df-run.yaml:226`), and operator v0.5.1 bypasses the warm pool for any
claim carrying env (`sandboxclaim_controller.go:1626-1627`), so a warm member is never
adopted — and on Nitro it holds a sandbox node while idle. Delivering run inputs without
claim env, so the pool becomes useful, is a follow-up (§8).

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
| `changeset` | Feeds `skipWhen` and auto-merge; a wrong answer skips gates | **Never** (for now — see the principle below) |
| Contract tests, licence scan, compliance tooling, model-graded rubric | Report a verdict. Block merge if the team put them in its process; cannot weaken anything else | **Yes** |


**Why team-authored steps are safe.** Three properties, each independently sufficient:

1. **No credentials.** No Kubernetes token, no IAM, no GitHub token, no holdout
   scenarios. A step cannot post a status, so it cannot forge one.
2. **No merge authority.** `platform/merge` requires the statuses the *platform* computed
   from the rendered process, plus policy's `requiredSteps`, which only platform steps can
   satisfy (D7). A team step can block its own team's merge; it cannot unblock anything.
3. **Isolated execution.** An agent sandbox (Kata or Nitro), default-deny egress, no control-plane reach
   (D4, D13).

**The principle for the reserved set.** A step is reserved *only* because a team
implementation could weaken a security control — never because it could merely be wrong.
`merge`, `approval`, `holdout`'s scenario mount and `security-scan` are that boundary.
`changeset` is reserved only until the platform evaluates skip decisions from a
team-supplied path list instead of trusting a step's answer, and holdout's evaluator only
until it can be given scenarios it cannot exfiltrate (§8). `platform/code` is not reserved:
a team may ship its own coder as a team step. The end state is a platform owning the merge
decision, the isolation substrate and the credential boundary, and nothing else.

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

**Testing a step.** A local runner, `df-step-test`, ships with the contract: it runs a step
image with a `DF_*` environment and a workspace from a local checkout, then validates the
result file. It tests platform steps too (D10). A **process** change is tested the way any
change to a factory is — by running it on a factory in a dev environment against a scratch
repo; there is no separate process simulator.

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

**Ownership.** Without the `shared/` tier, a team step is used only by its own team's
factories, so the factory's team owns it, including when its author moves on. Status shows
each step's owner and digest (D20), which is all an inherited factory needs to find it.

Making the registry an allowlist instead would put the platform back in the critical path,
which is exactly what D1 removes; the security properties come from D1's three invariants,
not from registration. The one gate that stays is `allowedRegistries` (D7) — a
supply-chain control, not a curation decision.

### D4. Team steps: images, executed on the sandbox substrate

**Decision:** a digest-pinned OCI image, run by a platform-owned runner
(`df-step-runner-v1`) as an agent sandbox in the factory namespace with
`automountServiceAccountToken: false`, the token-less executor identity, read-only root,
all capabilities dropped, and DNS-only egress unless the step was granted `egress: true` (D13).

| Option | Verdict |
|---|---|
| **An agent sandbox on the coder's substrate** (**chosen**) | The isolation already exists for the coder: one substrate, one `ClusterNetworkPolicy`, one posture to reason about. |
| Hardened ordinary pod | Probably sufficient given no credentials, and faster to start — but a second posture to maintain, and on EKS VPC-CNI a namespaced NetworkPolicy does not reliably apply to Sandbox-owned pods (D13), so the policy story diverges too. Reasonable per environment; not the default. |
| Reuse the coder warm pool | The pool is built from the coder image; a step needs its own. |

**The runner is platform code**, so D1's invariants are enforced at every step launch
rather than by each team's image behaving well. It mounts the workspace, writes `DF_*`,
launches the image, reads `$DF_RESULT`, and posts the status under the step's reserved
context (D7).

**Step pod shape.** A team image never runs as an Argo task: Argo's `wait` container needs a
ServiceAccount token, which in the same sandbox would reach the API server and every
`workflowtaskresult` in the namespace. Instead:

| Concern | Mechanism |
|---|---|
| Launch | The runner (a platform Argo task, SA `dark-factory`) creates a **`Sandbox`** for each step — an agent sandbox, never a bare pod — whose pod template it renders: `runtimeClassName` from the cluster class (§3.2) and no nodeSelector or toleration, `automountServiceAccountToken: false`, SA `dark-factory-executor`, label `role: step`. §3.2's render guards apply to it |
| Result | `terminationMessagePath: $DF_RESULT`; the runner reads it from the sandbox pod's status. Needs no token and no API reach from the step. Unparseable means `failure` (§3.1) |
| Workspace | A **per-launch PVC** (below). The step sandbox mounts it; the step pod holds **no** credential and needs **no** network to get its source. Scratch space outside it is an `emptyDir` with an explicit `sizeLimit` — the kubelet cannot see disk use inside a sandbox, so eviction would never fire (#151, `sandbox.cue:409-419`) |
| Logs | The step's output (§3.1). Read by the runner, linked from the check-run, tail sent to the coder on `revise` |

**Workspace delivery: a per-launch PVC.** The clone cannot happen inside the step pod: under
`stepEgress: none` that pod has DNS only, and with Nitro's `networkIsolated` it has no network
at all. So for each step launch that requests `workspace: true`, the runner:

1. creates a PVC `<run-key>-<step>` — `volumeMode: Filesystem`, `accessModes:
   [ReadWriteOncePod]` — from the platform's workspace StorageClass (D19, an EBS gp3 class
   with `WaitForFirstConsumer`), sized by `factoryPolicy.workspaceSize` and owned by the
   Workflow, so it is deleted with the run;
2. runs a short **clone pod** — platform code, an ordinary pod with the platform steps'
   GitHub egress — that mounts the factory's **reader** token (contents:read, D15) and the
   PVC, clones the branch, and exits. The reader token never enters a sandbox;
3. creates the step sandbox mounting the PVC. The volume's topology pins the sandbox to the
   clone pod's availability zone, which the scheduler applies on its own.

Each launch gets its own PVC because steps in a stage run in parallel, and an EBS volume
attaches to one pod at a time: on Nitro it is attached to the sandbox as a block device, and
AWS documents `ReadWriteOncePod` as the mode to use, since a block device can be attached to
only one sandbox. The clone pod, the install launch and the test launch use a volume **one
after another**, never at once, which that allows. Holdout's two launches therefore share one
PVC, and the installed tree passes from the install launch to the test launch with no copy
(D17). Volumes are never resized in place (Nitro does not support expansion while attached),
so `workspaceSize` is fixed per launch.

What is documented: Nitro Sandboxes mount EBS volumes through the EBS CSI driver in both
volume modes (on Auto Mode, provisioner `ebs.csi.eks.amazonaws.com`), and EFS is unavailable
under `networkIsolated` — which is why the workspace is EBS, not EFS. What is not documented,
and is §7's go/no-go: EBS under `networkIsolated`, and EBS mounts on the Kata pools.

| Option | Verdict |
|---|---|
| **Per-launch PVC, cloned by a platform pod** (**chosen**) | Works under `stepEgress: none` and `networkIsolated`; no credential in a step pod; Kubernetes-native, deleted with the run |
| Runner streams a tarball over `exec` | No volumes, but the runner needs `pods/exec`, workspaces flow through the API server, and it rules out `confidential` steps permanently |
| Clone inside the step pod | Needs GitHub egress in the step pod, which `stepEgress: none` and `networkIsolated` forbid |
| An OCI artifact or object store | A store and credentials to run; D16 bars AWS identity in a factory |

**Costs.** Per-step sandbox boot latency, paid in parallel across a stage but still paid —
plus a volume create, a clone pod and two attaches per workspace launch; a workspace launch
is limited to one availability zone;
and the quota must cover PVC count and storage (D20).
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
implement, verify (holdout, security-scan, and the DevOps Agent via `platform/ci-check`),
approve, merge — and the recommended starting point. A team may add its
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
  stepEgress: none          # none | public — whether any step may request egress (D13)
  workspaceSize: 10Gi       # per-launch workspace volume (D4)
  maxStages: 12
  maxStepsPerStage: 8
```

1. **Render.** A non-compliant process fails the Application naming the violated rule, so
   nothing deploys weakened. Covers `requiredSteps`, `minApprovals`, `allowedRegistries`, `stepEgress`, size
   caps, and that every step image is digest-pinned.
2. **Merge.** `platform/merge` requires the status of every step in the rendered process
   **and** every `requiredSteps` entry, declared or not — so deleting a required step does
   not produce a mergeable PR.
3. **GitHub.** A ruleset requiring the `dark-factory/*` checks from the platform App.

Each covers the others' bypass: render gives the good error message but is only reached
through the component; merge catches a hand-written Workflow that skipped a step; branch
protection catches one that skipped merge.

**Two rules exist specifically because teams author steps:**

- **`requiredSteps` may only name `platform/` steps**, or a team satisfies a
  platform-mandated security gate with its own stub. The most important guardrail here, and
  structural rather than configurable.
- **Verdicts are attributed, not named.** Platform steps own `dark-factory/<name>`, team
  steps `dark-factory/team/<step-name>`; the runner picks the name and the platform App
  posts it. Merge and the ruleset both check the App (§3.1), so naming a status
  `dark-factory/security-scan` from another token gains nothing.

Trust follows the tier rather than a prefix allowlist, which would conflate "which
external checks may `ci-check` wait on" with "which checks are trusted".

**Cost.** Policy lives in Helm values, invisible in the tenant's Application, and is
per environment — so one Application can be valid on one cluster and rejected on another.
Intended for dev versus production, but it makes promotion a thing to test.

### D8. Failure handling, fast paths, and one iteration budget

| `onFail` | Behaviour |
|---|---|
| `stop` (default) | The run fails; statuses stay red; the PR is not mergeable |
| `revise` | Failing steps' `summary` and log tail go back to the coder, consuming one iteration |
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

**Concurrency.** `maxConcurrentRuns` is an Argo semaphore in the namespace, so the limit is
per factory, not per repo (per-repo limits are additive, §8); the per-issue
mutex is keyed on the run key (D21) so a revision run and its original serialize
instead of each claiming a VM.

**Human gates end the run.** `platform/approval` is a stage boundary, not a wait inside the
DAG: the run posts `dark-factory/approval=pending`, releases the VM claim, mutex and
`maxConcurrentRuns` slot, and ends. A GitHub review event starts a **continuation run** from
the next stage, keyed on the same run key (D21); it re-reads the PR head and re-checks every
earlier status there, so a push after approval invalidates it. This is how the hub chart's
merge workflow already works. Stages after an approval are therefore rendered as a second
entrypoint of `dark-factory-process` (D5).

**Cost:** a process with N approvals is N+1 runs, and status must stitch them into one view
(D20).

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

The version names the **interface**, not the artifact: a compatible fix ships as a new
digest inside the same `df-step-<name>-<version>`, so factories pick it up without bumping
anything. What is immutable is each run: the template pins one digest at any moment, the run
records which, and status reports it (D20).

**Cost:** three things carry versions (contract, platform steps, team images). Status
reports the resolved set (D20).

### D10. Platform steps are images too

Platform steps run logic adapted from the `dark-factory` chart (`merge.js`, `iterate.js`,
`security-wait.js`, `evaluate.js`, `holdout-gate.sh`), which that chart mounts from a
ConfigMap — and a ConfigMap can only be **mounted** in its own namespace. (Reading one by
name, which D11 relies on, is a different path: the controller resolves `configMapKeyRef` in
the Workflow's namespace.)

| Option | Verdict |
|---|---|
| **One image per platform step, digest-pinned in its template** (**chosen**) | Same packaging as a team step, so there is one execution model, one contract, one way to test. |
| Scripts embedded in the templates as Argo raw artifacts | No image build, but it makes platform and team steps structurally different things — see below. |
| One image carrying every platform step's scripts | Fewer artifacts, but a change to `merge` re-releases `holdout`, and per-step versioning (D9) stops meaning anything. |

**Why images, when embedding scripts would avoid a build.** Because the two tiers being
the *same shape* is worth more than the build it saves:

- **It makes the reserved set movable.** D1's principle has `changeset` and holdout's
  evaluator becoming team-ownable. If platform steps are images honoring the contract,
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
`df-step-merge-v1` resolves to one known digest at any moment, and every change to it is a
reviewed platform commit, never a tag moving under it. Tenants still cannot change it: the
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

**Outside our accounts**, the digests in the shipped templates are placeholders: this repo is
public, and an operator builds or mirrors the images and re-pins them through chart values.
A digest left at its example value fails render, by the same placeholder guard #151 applies
(§3.2), so a forgotten re-pin is loud.

**Costs.** A build-and-publish pipeline we did not have, and several small images instead of
one.

### D11. Factory-wide settings in a ConfigMap

Parameters default from `dark-factory-config` via `valueFrom.configMapKeyRef`, which Argo
resolves in the **Workflow's** namespace, so one template serves every factory. Keys are
only genuinely factory-wide: `coder-engine`, `bifrost-url`, `max-iterations`,
`max-concurrent-runs`, `run-timeout-seconds`. Claim TTLs are not here: the claimer reads them
from the coder pool's card (§3.2). Per-step tuning travels in
`DF_CONFIG`. The alternative — the Sensor injecting every setting as an argument — makes
every trigger carry every setting.

Settings are read when a run starts, so a change applies to the next run. The ConfigMap
needs the label `workflows.argoproj.io/configmap-type: Parameter` or Argo ignores it;
verified (§7 evidence) that `argo-cluster-role` grants the controller
`configmaps get/list/watch`. The design rests on one unobserved behaviour here — that these
defaults resolve in the Workflow's namespace under `clusterScope: true`; §7 makes it a
go/no-go gate and names the fallback. A step a team does not want is simply absent from its process,
so there are no `*-enabled` keys and no skipped nodes in the UI.

### D12. Fixed contract names; one factory per namespace

Fixed names keep templates readable; deriving them from a `factory` parameter would make
every name an expression, and some fields (template-level executor SA, ConfigMap refs used
to *resolve* parameters) cannot be safely templated. The namespace is Kubernetes' isolation
boundary for RBAC, Secrets, NetworkPolicy and quotas — two factories sharing one would
share credentials, blast radius, and now two teams' step images in one isolation domain.

The workflow ServiceAccount is `dark-factory`, not `context.name` as the OAM authoring rules
prescribe ([`.kiro/steering/oam-authoring.md` §3](../../.kiro/steering/oam-authoring.md): "A
component that runs a workload owns its ServiceAccount, named `context.name`"): it must never gain an AWS identity (D16), so not being a trait anchor is a
feature.

### D13. Control-plane isolation keyed on a pod label

On EKS VPC-CNI an ordinary NetworkPolicy does not reliably apply to Sandbox-owned pods, so
the real isolation is an Admin-tier `ClusterNetworkPolicy` selecting
`agent-sandbox.io/role in (coder, step)` in any namespace — covering every factory's coder
and every team step automatically. The component stamps `role: coder` on the coder pool and
the runner stamps `role: step` on each step sandbox, so coverage does not depend on the image
cooperating — and a coder rendered with `type: sandbox`'s `session` role would fall outside
it, which is why §3.2 sets the role itself. A per-factory policy would mean a tenant
Application owning cluster-wide admin-tier network policy.

The component also keeps a namespaced default-deny egress policy as defence in depth.

**Step egress is by address range, not by host.** The label-keyed Admin policy for
`role: step` always denies the cluster's service and pod ranges, the VPC's private ranges
and the instance metadata endpoint, and allows DNS. A step granted `egress: true` is stamped
`agent-sandbox.io/egress: public` by the runner, and a second rule allows that label TCP 443
to any remaining address. Everything else is denied. These are CIDR rules, which
`ClusterNetworkPolicy` enforces with VPC CNI 1.21.1 or later. EKS documents network policy
for **EC2 Linux nodes** only, so this is not "every node type": it holds on the Kata pools
(self-managed Karpenter on EC2) and, per #151, on Nitro under Auto Mode, but each runtime is
confirmed by §7's per-runtime test before `stepEgress: public` may be enabled on a cluster.
The service range must be in the deny list explicitly, because VPC CNI matches egress before
Service address translation.

**Start-up window.** In VPC CNI's default `standard` enforcing mode a new pod is
default-allow until its policies are programmed (observed in `30-networkpolicy.yaml:48`).
Requiring `NETWORK_POLICY_ENFORCING_MODE=strict` would close it, but strict mode is
cluster-wide and needs a policy for every pod. Instead, a step launch whose pod holds
anything confidential — holdout's test launch (D17) — starts with an init container that
probes a public address and does not exit until the probe is refused, so the main container
never runs inside the window. On Nitro, that launch uses `networkIsolated` instead: the
network device is absent rather than filtered, so there is no window to wait out.

**Why not per-host lists, yet.** EKS's `ClusterNetworkPolicy` and `ApplicationNetworkPolicy`
accept `domainNames`, but AWS enforces DNS-based rules only on EKS Auto Mode-launched nodes;
elsewhere they are accepted and silently not applied. The factory assumes #151's default
path — Nitro classes shipped by Auto Mode, with no node layer of ours — and Kata on
self-managed Karpenter as the annotated alternative. On a Kata cluster a `domainNames` rule
would silently not apply. On the Auto Mode Nitro path, per-host egress becomes an option once
§7 confirms D13's rules, and `domainNames`, are enforced for a Nitro guest (§8). A forward
proxy would work everywhere but is a new service to run.

| Option | Verdict |
|---|---|
| **CIDR rules, public-or-none per step** (**chosen**) | Enforced on both runtime paths, confirmed per runtime in §7; nothing new to run |
| `domainNames` in the Admin policy | Enforced only on Auto Mode nodes; silently open on Kata. A later option for Auto Mode Nitro clusters |
| A platform forward proxy | Works everywhere; a new service and a new failure point |
| No egress at all | Most test steps need a package registry |

**Cost:** a step with `egress: true` can reach any public host, so it can send out whatever
it can read (§5). Environments that need a host allowlist apply one at the VPC (AWS Network
Firewall or Route 53 Resolver DNS Firewall), outside the factory.

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

### D15. One platform GitHub App; four short-lived tokens per factory

The platform registers **one GitHub App per environment**; each team installs it on the
repos its factory watches. Its private key is held **only in a platform namespace** — never
in a factory namespace — because anything holding it can mint tokens for every repo the App
is installed on. The App's permissions exclude `administration: write`, so no token minted
from it can change the ruleset that enforces its own checks (D20).

Each factory gets four installation tokens, each **down-scoped** to the factory's repos and
to one role's permissions. They are produced entirely with External Secrets, so there is no
platform service to run and the key never leaves the platform namespace:

- Per factory, one `GithubAccessToken` **generator per role** in the platform namespace. Each
  references the App key, which sits in the same namespace, as External Secrets requires of
  anything a generator uses, and carries that factory's installation ID, repo names and the
  role's permissions — the generator, not the `PushSecret`, is where a token's scope is set.
- Per factory, a **`SecretStore` of External Secrets' Kubernetes provider**, also in the
  platform namespace, with `remoteNamespace: dark-factory-<team>`. For this provider the
  "remote store" is the Kubernetes API itself: pushing to it creates or updates an ordinary
  `Secret` in that namespace. It authenticates as a ServiceAccount whose Role in the factory
  namespace may write only the four `dark-factory-github-*` Secrets (`resourceNames`).
- Per factory, one **`PushSecret` per role** in the platform namespace, with
  `selector.generatorRef` pointing at that role's generator and `secretStoreRefs` pointing at
  that store. Each
  refresh mints a new token and writes it as `dark-factory-github-<role>` (§3.2), well inside
  the hour a token lasts.

So the chain is generator → `PushSecret` → Kubernetes-provider `SecretStore` → native Secret in
the factory namespace. Nothing in the factory namespace pulls: pods mount the Secret like any
other, and the namespace holds no generator, no key and no External Secrets objects for these
tokens — only the four Secrets. The generator runs only beside the key, so the key never
needs to be anywhere else; only the minted output crosses into the factory, written by the
External Secrets controller, so no factory pod holds a Kubernetes token (D4, D16).

**What bounds a namespace to its repos.** Every object above is created by the platform's
**onboarding** (D20), not by the Application, and all of them live in the platform namespace,
where no team has access. Together they are the factory's **onboarding record**: the repos
and installation IDs a token may name. Editing `repositories` in an Application cannot widen
what a token reaches; a repo in the Application but not in the record gets no token, and
status reports it (D20).

| Token | Used by | Scope |
|---|---|---|
| coder | The coder VM (0400, never in env) | Issues read; contents and PRs write. **No checks** — `platform/code` posts for it |
| orchestrator | **Platform steps only** | Adds `checks: write` and Issues write. The only token that can post a verdict merge accepts |
| events | EventSource only | Repository webhooks write, for webhook registration. The HMAC secret still comes from Secrets Manager |
| reader | D4 clone pods only — platform pods, never a sandbox | Contents read. Nothing else |

**Who can merge.** GitHub has no separate merge permission: any token with contents write —
the coder's and the orchestrator's — can *call* the merge endpoint. Whether the call
*succeeds* is the ruleset's decision, and it requires every `dark-factory/*` check from the
platform App, approval included, to be green at the PR head (§3.1, D20). The coder token
cannot create those checks, so it cannot merge anything a human has not approved and every
gate has not passed; a push after approval moves the head and leaves the new head
unchecked. `platform/merge` is simply the step that makes the call once they have.

**Why minting happens in the platform namespace.** External Secrets' generators, including
the cluster-scoped `ClusterGenerator`, require the objects they reference — here the App key —
to be in the same namespace as the ExternalSecret they fill. Minting directly into a factory
namespace would therefore put the key in every factory, which is exactly what this decision
forbids. Minting beside the key and delivering only the result avoids that.

| Option | Verdict |
|---|---|
| **One platform App; External Secrets mints in the platform namespace and pushes through a scoped Role** (**chosen**) | GitHub enforces who posted a verdict; every token is short-lived and down-scoped; no machine users; the key stays in one namespace; nothing new to run |
| The same, minted by a platform token service or CronJob | Same security, but platform code to build and operate. The fallback if External Secrets cannot do the above on a hub |
| External Secrets' generator in each factory namespace | One hop, but the App key must sit in every factory namespace |
| Fine-grained PATs, merge checks the poster's account | No App to install, but GitHub checks names only, tokens are standing, and the orchestrator needs a dedicated machine user |
| One App per factory | Smaller key blast radius, but the key sits in a namespace that runs untrusted code — a coder escape could forge its own factory's verdicts — and every team registers and operates its own App |

**Cost:** each team installs one more App per repo; the private key is a high-value secret
whose blast radius is every installed repo; onboarding creates nine External Secrets
objects per factory (four generators, four `PushSecret`s and a `SecretStore`); the External Secrets controller can read every minted token (as it can
every Secret it manages already); if minting stops, tokens expire within the hour and runs
fail closed; and status must report an App missing from a watched repo (D20).

Team steps get none of these (D1); one needing its own credential declares `secrets:` and
gets keys from the factory's `stepSecrets/` prefix, which the team controls and which never
holds a GitHub token minted from the platform App. The most exposed components hold the least privilege, and
the cluster prefix lets each cluster's External Secrets identity be limited to its own
paths.

### D16. No AWS identity anywhere in a factory

No factory ServiceAccount carries IAM, and `factoryPolicy` forbids attaching an identity
trait to one. A team step's pod sees no GitHub credential at all: the D4 reader token is
mounted only in the platform clone pod that fills the step's workspace volume.
`platform/security-scan` waits for the Security Agent **GitHub App** and
mirrors its verdict rather than calling AWS APIs; the chart's IRSA-plus-S3 mode is not
ported. The coder and every team step execute untrusted code in a shared namespace, so
having no AWS credentials there at all is simpler and stronger than scoping them per step.
A per-step scoped identity for the Security Agent's headless API path was considered and
rejected for v1: it still lives on a ServiceAccount in a namespace that runs untrusted code,
and that path needs account-specific agent-space and role values the public chart must not
default. Revisit if the App path proves unworkable.

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

**Execution.** `platform/holdout` splits in two. The **tests**, which run coder-written
code, go through the D4 runner like a team step: an agent sandbox with no token, the workspace,
and **only this repo's scenario key** mounted (a Secret volume `items:` entry set from the
run's `<owner>-<repo>`). The **judge and verdict** stay in the platform step, which reads
the test launch's verdict and log from the runner and posts the result. Coder output never shares a pod with a credential or with
another repo's scenarios.

The test launch **never gets egress**, whatever `stepEgress` says: the scenarios are mounted
there, and a test with network access could send them out. It also never starts inside the
pod-start default-allow window: on Nitro it runs `networkIsolated`, with no network device at
all; on Kata its first init container, which does not mount the scenarios, waits until
egress is refused (D13), and only then does the test container that mounts them start.

Dependencies are installed in a preceding launch that has the workspace but no scenarios.
That launch gets public egress only where `stepEgress: public` allows it. Under the shipped
`stepEgress: none` it gets DNS only, so a repo's dependencies must be vendored in the
repo or present in the test image; a repo that needs a registry fails holdout with a status
naming `stepEgress` as the cause, rather than passing untested. The two launches share one
workspace PVC and run in sequence, so the installed tree is simply there when the test launch
mounts it (D4) — which also works under `networkIsolated`, since neither launch clones.

**Cost:** one runner launch per holdout run, and the evaluator splits into a test image and
a judge.

### D18. `ci-check`: wiring checks that already run elsewhere

`platform/ci-check` waits (bounded) for a named GitHub check and declares it so merge
requires it — for checks a team already runs outside the factory:

```yaml
- {use: platform/ci-check, config: {check: "ci/unit-tests"}}
```

It is **not** the path for bespoke tooling; that is a team step (D1), which can see the
workspace, feed `onFail: revise`, and run inside the factory. A timeout is a failure, hence
a refusal at merge.

The DevOps Agent is wired this way: `standard` includes
`{use: platform/ci-check, config: {check: "aws-devops-agent/release-readiness-review"}}`. On
timeout the status names the likely cause (repo not connected to the Agent Space), so a
missing App fails loudly rather than as a silent refusal. The chart's `devopsAgent.gate:
label` mode is dropped; a team using the coding-agent plugin wires its own check.

### D19. Platform-supplied environment values

Developers never set account, region, cluster name, webhook host, or Bifrost's address.
These flow from the cluster secret's annotations through the addon registry to Helm globals
(`awsAccountId`, `awsRegion`, `clusterName`, `bifrostUrl`, `bifrostClusterIp`,
`ingressDomain`, and `workspaceStorageClass` for D4's workspace volumes) and into CUE
defaults. Bifrost's address is **one cluster value**, as in
#151: the pinned ClusterIP by default, overridable with the cluster's `bifrost_url`
annotation. The cause is the sandbox operator's `dnsPolicy: None`, which applies on every
runtime, not anything Kata-specific, so there is no per-runtime value. The ClusterIP is
admitted by a `/32` egress rule, because VPC-CNI matches egress on the pre-DNAT ClusterIP,
and must stay in sync in three places: the bifrost chart's `staticService.clusterIP`,
agent-sandbox's `networkPolicy.bifrostClusterIp`, and this value. `webhook.host` defaults to
`ingressDomain`, and factories are told apart by D14's `/dark-factory/<ns>/<name>` path,
as the hub factory does today (`registry/sandbox.yaml`). A per-factory host would sit two
labels below the domain, which a single-level wildcard certificate does not cover. Override
it only with a host the cluster's certificate already matches.

### D20. Status, and prerequisites the platform owns

An Application that claims to describe the factory, beside a factory that silently fails
because a secret is missing, is an abstraction leak. With team-authored steps status
carries more weight still: it is where a team sees that its own step is breaking its
factory.

`customStatus` and `healthPolicy` report the **expanded** process (stages, steps, each
step's tier, resolved platform versions and team digests), each prerequisite's state (the
App installation and the four minted tokens, the holdout source per repo, warm pool readiness, webhook registration, step
images resolvable), and which `factoryPolicy` applied. The platform also takes over
namespace creation, `webhook.host` defaulting (D19), and the holdout source as a
secrets-store reference (D17); the team keeps what lives in GitHub — the Apps, branch
protection — and its own step images.

**Pool health uses #151's policy:** `readyReplicas >= spec.replicas` on the
`SandboxWarmPool`, which has both fields
(`extensions.agents.x-k8s.io_sandboxwarmpools.yaml:40,66`) and which #151 verified live
(`sandbox.cue:67`). With the default `warmPoolSize: 0` it is trivially healthy; the
prerequisites below carry the real signal.

**Onboarding and tenancy.** The namespace is the tenant boundary, so the team must not
administer it. Onboarding is an entry in the `dark-factory-templates` chart's
`onboarding.factories` values, reviewed and synced like any platform change. It writes the namespace's **onboarding record** — the
platform-namespace External Secrets objects that mint and push its tokens, plus the scoped
Role (D15) — and creates `dark-factory-<team>` with:

- a team Role that may apply and read the `Application`, and read Workflows, pods and logs —
  **no** Secrets, Pods, Workflows or RBAC writes;
- **one admission policy on Applications.** Applying an Application is not narrow on its
  own: vela-core applies with its own identity unless `authentication.enabled` is set
  (default `false`), and `k8s-objects`, `ref-objects`, the `json-patch` and
  `json-merge-patch` traits, policies and workflow steps can each make it create or rewrite
  objects the team Role denies. In a factory namespace an Application is admitted only if
  every component is `type: dark-factory` with **no traits**, and it has **no `workflow`
  steps and no `policies`**. vela then can only create what the platform-written
  `dark-factory` definition renders, and that definition exposes **no pass-through pod
  fields** — no raw pod spec, volumes, env-from-secret or labels; properties are typed values,
  and team images run only through the runner (D4).

  This is the whole tenancy control: the team can create nothing but its Application, and
  its Application can produce only the platform's pods, so no team-controlled path yields a
  pod that mounts a factory token. There is deliberately no separate per-Secret mount
  policy; what remains is a cluster administrator granting a team more RBAC, which §5
  already lists;

  **Delivery through GitOps is recommended, not required.** A team should commit its
  Application to a repo that ArgoCD syncs, with the team's own review. Platform review of
  those changes is optional and is not a substitute for the admission rule: the rule's
  concern is not extra resources, which the quota caps, but a few lines — a `json-patch`
  trait, a `k8s-objects` pod — that would mount the orchestrator token and forge every gate.
  That is a small diff in a routine-looking change, which review catches unreliably, and
  making platform review mandatory would put the platform back in every team's process
  changes (D1, D3). ArgoCD also applies with its own identity, and an AppProject restricted
  to the `Application` kind cannot see inside one, so the rule holds whichever path applies
  the Application;
- a ResourceQuota sized for the warm pool plus `maxStepsPerStage` step pods and their
  workspace PVCs (count and `requests.storage`), **including
  each pod's RuntimeClass overhead**, which counts toward quota: on Nitro, `overhead.podFixed`
  of 256Mi (isolated) or 512Mi (confidential), and one `eks.amazonaws.com/nitro-sandbox`
  slot per pod.

**The ruleset is the hard barrier.** A GitHub ruleset is enforced by GitHub on every push and
merge — API, UI or git — so it is what stops a merge no factory component should make
(D15). It holds only if the default branch's ruleset has all of:

| Setting | Required value | Why |
|---|---|---|
| Required status checks | Every `dark-factory/*` check of the rendered process and `requiredSteps`, each pinned to the **platform App** as its source | A same-named check from any other source does not satisfy it (§3.1) |
| Force pushes, deletions | Blocked | History cannot be rewritten around the checks |
| Enforcement | **Active** | "Evaluate" only logs; it blocks nothing |
| Required approvals | At least `process.approvals` and `factoryPolicy.minApprovals`; stale approvals dismissed on push; code-owner review where the team uses CODEOWNERS | `platform/approval` reads GitHub's `reviewDecision`, which GitHub computes from these rules, so the step and the ruleset cannot disagree. Restricting approvers to a team is done with CODEOWNERS, not step config |
| Bypass list | **Empty** | Any bypass actor skips every rule |
| Level | **Organization ruleset** (recommended) | Repo admins can edit or delete a repo ruleset; only organization owners can change an organization one |

The platform App must **not** hold `administration: write`, or a token minted from it could
weaken the ruleset; reading it is all the check below needs. Organization rulesets on private
repos need GitHub Team or Enterprise; where only a repo ruleset is possible, the team's repo
admins can still switch it off, and the check below is what notices (§5).

**The ruleset is checked, not assumed.** At startup and before every merge, the factory
reads the default branch's rules with the orchestrator token and reports in status whether
the platform App is installed and every row above holds. Any gap fails closed: runs start,
merge refuses, and status names the missing setting.

**Cost:** status needs state from objects the component does not own, so `customStatus`
expressions get long and brittle. A reconciler would be better (§8).

### D21. Smaller choices

| Choice | Reason |
|---|---|
| Run key is `<owner>-<repo>-<issue-number>` | The number is in every payload, including a revision run resolved from a PR, which `issue.id` is not. The bare number is unique only per repo, and a factory watches several, so `acme/api#7` and `acme/web#7` would share a mutex and a claim. Repo names allow `_` and `.`, which DNS-1123 labels reject, and joining with `-` is ambiguous (`acme-api/web` and `acme/api-web` both give `acme-api-web`). So the key is lowercased, every character outside `[a-z0-9-]` becomes `-`, and an 8-character hash of the exact `owner/repo#number` is **always** appended, truncating the readable prefix so the whole key fits 63 characters. |
| `synchronization.mutexes` (list) | Argo v4 removed `mutex`. Verified (§7 evidence): v4.1.4 rejects it; the list form works on v3.6+. |
| No issue title in the claim | Raw issue text broke the claim manifest on quotes. The coder reads the title from the issue it fetches. |
| Revision notes base64-encoded | Arbitrary markdown cannot break the claim manifest. |
| Deterministic Workflow names per event | GitHub redelivers webhooks; duplicates collide (`AlreadyExists`) instead of racing. |
| Registry gate `enable_dark_factory` | One label already turns on the Dark Factory stack; the step templates join it. |

---

## 5. Security model

| Threat | Control |
|---|---|
| Coder code reaches credentials | Agent sandbox (own kernel); no Kubernetes token, no IAM; only a narrow, hour-long GitHub token at `/etc/secrets` (0400) |
| **Team step reaches credentials** | **No tokens mounted (D1, D15); only declared `stepSecrets` keys, which the team owns** |
| Coder or team step reaches the control plane | Admin-tier ClusterNetworkPolicy on `role in (coder, step)` (D13); DNS-only egress unless granted; private ranges always denied (D13) |
| **Team step forges another step's verdict** | **Steps never post statuses; the platform runner reads `$DF_RESULT` and posts under a tier-namespaced context it chooses (D2, D7)** |
| Another token posts a same-named passing status | Merge and the ruleset accept only check-runs from the platform App; only the orchestrator token can create them (§3.1, D15) |
| **Team step satisfies a required gate with a stub** | **`requiredSteps` may only name `platform/` steps — structural, not configurable (D7)** |
| **Hostile step image** | **Digest-pinned from an allowed registry (D7); agent sandbox, no tokens, read-only root, caps dropped, DNS-only egress unless granted (D4, D13). Worst case: that team's own PRs fail to merge** |
| Coder games its own tests | Hidden scenarios no step but `platform/holdout` can reach (D17); a judge from a different model family; fail-closed on missing scenarios |
| Coder output executed on the hub (holdout) | Runs through the D4 runner: no token, one repo's scenarios, agent sandbox (D17); `npm install --ignore-scripts`; no AWS identity in the namespace (D16) |
| Holdout's test launch runs before its egress policy applies | `networkIsolated` on Nitro; on Kata an init container holds the test container until egress is refused (D13, D17) |
| Team creates arbitrary objects through its Application | Admission admits only `type: dark-factory` components with no workflow steps or policies (D20) |
| Team pod mounts a factory GitHub token | The team can create only its Application, which admission limits to `type: dark-factory` with no traits, workflow steps or policies; that definition exposes no pod fields (D20) |
| A factory obtains tokens for repos it was not onboarded for | Tokens are minted in the platform namespace from the onboarding record, which no team can write; the push is bounded by a `resourceNames`-scoped Role to that factory's own token Secrets (D15) |
| Unreviewed or failing code merged | Merge only on a human approval event; `platform/merge` refuses unless every required status is green at the PR head; a missing status is a refusal |
| Tampered platform step code | Each platform step's image is digest-pinned in a cluster-scoped, platform-owned template (D10); only the platform changes the digest, through a reviewed commit; each run records the digest it ran (D9) |
| Tenant process drops a required gate | Three overlapping enforcement points (D7) |
| Runaway revision loop | One iteration budget shared by `onFail: revise` and comments (D8); factory-authored comments filtered at the Sensor |
| Two runs for one issue holding two VMs | Mutex and claim keyed on owner, repo and issue number (D21); `maxConcurrentRuns` semaphore (D8) |
| A step reaches another factory | The `role: step` Admin policy always denies the cluster's service and pod ranges and the VPC's private ranges; granted egress is public addresses only (D13) |
| Cross-factory interference | One factory per namespace (D12); per-namespace events and secrets (D14, D15); Argo locks are namespace-scoped |

**Residual risks**

- **The ruleset is the only hard stop on self-merge** (D20). Rulesets on private repos are
  unavailable on GitHub's free plan. With a repo-level ruleset, the team's repo admins can
  switch it off; the factory then refuses to merge, but a human could still merge in the UI.
  An organization ruleset removes that gap.
- **Render-time policy binds only the component path.** Team RBAC removes raw Workflow
  submission for the team (D20); a cluster admin can still bypass it.
  `templateReferencing: Strict` would close it cluster-wide (§8).
- **A valid process can still be a bad process**, and team steps widen this: a team can
  compose something that passes policy and verifies very little, or whose own steps always
  return success. Policy floors *our* gates, not the quality of a team's.
- **A team step can exfiltrate the source it is given.** With `workspace: true` and
  `egress: true`, a step sees the branch and can post it to any public host — v1 has no
  per-host limit (D13). Inherent in letting teams run code against their own source, which
  is why `stepEgress` is per-environment policy rather than a team setting, and why an
  environment needing a host allowlist applies one at the VPC.
- **The App private key is the highest-value secret in the system.** It can mint any
  permission the App has on every installed repo, across every factory. It lives only in a
  platform namespace, read only by External Secrets' generator (D15); a compromise there is a
  compromise of every factory.

---

## 6. Operations

**Platform, once per hub.** Register the platform GitHub App, store its private key in
the platform namespace beside the External Secrets generator that mints from it (D15). The sandbox runtime comes
from the cluster, as for `type: sandbox`: Nitro needs nothing; a Kata cluster sets
`sandbox_runtime: kata` and the OAM runtime-class annotations on its cluster secret (#151
§7.2). Make the platform step images pullable in this hub's account,
so the digests their templates pin resolve (D10). Set `enable_dark_factory=true`, which
syncs `dark-factory-templates` plus the agent-sandbox stack. Set `factoryPolicy` for the
environment (D7) — the shipped default is production-shaped: `platform/security-scan`
required, one approval, no registries allowed until the operator names them, and
`stepEgress: none`. Enable
`enable_oam_components`, then regenerate the component YAML with `platform/oam/generate.sh`
and **commit the result**: `oam-agent-components/templates/dark-factory.yaml` is a
generated, checked-in file like the other components', and the chart cannot register the
type without it.

**Team, per factory.** Apply the Application, starting from `preset: standard`. Install the
platform App and the Security and DevOps Agent GitHub Apps on each repo (or leave those steps out, if policy
allows). Have an organization owner apply the D20 ruleset, or apply it at repo level where
the plan allows only that; status reports any setting that is missing. Seed the
holdout scenarios at the paths status reports as missing; the tokens are minted, not seeded. The DevOps
Agent has no API path: connecting a repo to an Agent Space is console work
([`AGENT-INSTALL.md`](AGENT-INSTALL.md) A1–A3), a vendor constraint rather than a design
choice.

**Team, per step it owns.** Build an image whose entrypoint honors §3.1 — any language, no
SDK. Push it by digest to a registry in `allowedRegistries`. Reference it in `process` with
the capabilities it needs, and bump the digest to release.

**Changing the process.** Edit `process`; KubeVela re-renders and the next run uses it.
In-flight runs finish on the old shape. A violation of `factoryPolicy` fails at render.

**Upgrading platform steps.** A compatible fix keeps its version; an incompatible change
ships at a new one, and factories move by bumping `catalogVersion` or that step's `version`,
with the old template kept until every factory has moved. Team step upgrades are the team's
own digest bump (D9).

**Deleting a factory.** Deleting the Application removes everything the component created in
the namespace:

- **In-flight runs** are deleted with the namespace's Workflows; their SandboxClaims go with
  them, and the card's `maxLifetime` (§3.2) reaps any sandbox the deletion misses.
- **Webhooks** registered by the EventSource are deregistered on its deletion; status
  reports any it could not remove.
- **Minted tokens expire within the hour**, and stop being refreshed once offboarding
  removes the namespace's onboarding record. The team uninstalls the platform App from
  repos no other factory watches.
- **Open `df/` PRs and branches stay open**, so work in progress is not lost silently; their
  `dark-factory/*` checks can no longer turn green.

The namespace itself is removed through onboarding (D20), not the Application.

---

## 7. Validation

Already established, on the substrate rather than this design's shape: `mutexes` accepted
and `mutex` rejected on Argo v4.1.4; a server-side dry run on `dora-showcase` for the
component's namespaced objects (Sandbox and ExternalSecret CRDs are absent there); and the
holdout evaluator against sample scenarios with a stub judge that always says yes — an
honest implementation passed 4/4, a hard-coded one failed 2/4, to be re-run in the
`platform/holdout` image (D10).

Needed, none of it done:

- **Token minting (D15):** a namespace receives tokens only for the repos in its onboarding
  record; a repo added to the Application but not the record gets none and appears in
  status; a factory's push SecretStore can write only that factory's four token Secrets and
  cannot read the App key; tokens are refreshed before expiry and stop after offboarding.
- **Go/no-go: workspace PVCs on each runtime (D4).** Nitro mounting EBS is documented; still to
  confirm are a Nitro step sandbox mounting the workspace PVC **under `networkIsolated`**, and a
  Kata step sandbox mounting it, each after a clone pod has released it, in the clone pod's
  zone. If a runtime cannot, workspace steps on it fall back to the runner streaming the
  workspace over `exec` (D4's options table).
- **Tenancy (D20):** an Application with a `k8s-objects` or `ref-objects` component, any
  trait (including `json-patch` and `json-merge-patch`), a `workflow` step or a `policies`
  entry is rejected in a factory namespace; and no `dark-factory` property can reach a
  rendered pod's spec, volumes, env sources or labels.
- **Go/no-go: `configMapKeyRef` defaults resolve in the Workflow's namespace** for a
  Workflow submitted against the namespaced `dark-factory-process` whose tasks are
  `templateRef` with `clusterScope: true`. Gates any factory beyond the
  hub. If it fails, the fallback is for the component to render settings into
  `dark-factory-process` as literal parameter defaults, which it already renders per factory
  (D5).
- **A conformance suite for the step contract** — a reference step in two languages, plus
  adversarial ones: no result file, malformed JSON, non-zero exit, a result claiming
  `success` after an internal error, a step attempting to post a GitHub status, a step
  reaching for the API server and the holdout Secret. The last three must fail closed. This
  suite is the executable form of D1's three invariants. Also: a passing `dark-factory/*`
  status or check-run not created by the platform App must be refused by `platform/merge`
  (§3.1).
- CUE type-checks: each preset, `addSteps`, an explicit process, a violation of each
  `factoryPolicy` rule (must fail naming the rule), a tag-not-digest image, and a
  `requiredSteps` entry naming a team step (must be rejected).
- A rendering golden test per preset: stage order, step set, and the required-check list
  `platform/merge` receives.
- Per-platform-step tests against the contract, each starting with empty `DF_PR_NUMBER`.
- `helm lint` and `argo lint` on every platform step template and the runner; every
  `configMapKeyRef` key present in the rendered `dark-factory-config`.
- Both Admin ClusterNetworkPolicies admitted with distinct priorities on a live VPC-CNI, and
  **per runtime** (Kata on Karpenter, Nitro on Auto Mode) that `role=step` covers
  runner-launched sandboxes and that D13's CIDR rules are enforced for the guest — the gate
  for enabling `stepEgress: public` on that cluster. On Nitro, also whether `domainNames`
  rules are enforced for a guest (§8).
- Holdout's test launch: on Kata, the init gate holds the test container until a public
  probe is refused; on Nitro, `networkIsolated` leaves no network device (D17).
- #151's render guards (§3.2) for the coder pool and runner-launched steps, as rows in
  `scripts/test-isolation-matrix.sh`.
- `scripts/nitro/verify-isolation.sh` against a claimed coder and a runner-launched step,
  showing each runs in its own guest kernel.
- A step result over 4 KB, or malformed, is reported as `failure`; a step's full log is linked from its check-run (§3.1).
- Sandbox cold-boot latency per runtime for a trivial step image, to decide whether §2's "no warm pool"
  holds.
- A live run on `preset: standard` (label → VM → PR → steps → approval → merge), then one
  with a team step and `onFail: revise`.

---

## 8. Follow-ups

1. **The `shared/` registry tier** — promotion, review and ownership for a team step others
   depend on.
2. **Cosign verification of step images**, alongside `allowedRegistries`.
3. **A warm pool for step execution**, if sandbox boot latency proves to matter.
4. **Move the chart's hub factory onto the step templates**, removing the last duplicated
   pipeline. **Port the chart's templates to `mutexes`** — they fail on Argo v4 today.
5. **Evaluate `templateReferencing: Strict`** on the hub's Argo controller — the only thing
   that would close the inline-Workflow bypass.
6. **Update `setup-secrets.sh`** for the App key, per-factory paths, the holdout source and
   `stepSecrets`.
7. **Implement D10:** split the chart's scripts into one image per platform step, each
   honoring the contract; pin each by digest in its template; stand up the build-and-publish
   pipeline; delete `examples/dark-factory/steps/` and `stepsImage`.
8. **A reconciler for status** instead of long `customStatus` expressions (D20).
9. **Per-host step egress**, through `domainNames` on clusters on the Auto Mode Nitro path
   once §7 confirms it is enforced for a guest, or a VPC firewall elsewhere (D13).
10. **Shrink the reserved set** per D1's principle: `changeset` decided from a team-supplied
    path list, and holdout's evaluator as a team step given scenarios it cannot exfiltrate.
11. **Per-repo concurrency limits** alongside the per-factory `maxConcurrentRuns` (D8).
12. **Deliver run inputs without claim env**, so the coder warm pool is actually adopted and
    `warmPoolSize` can default above 0 (§3.4).

---

## Appendix: files

Planned layout. This branch holds only this design doc; of the paths below, only the chart
sources, `31-clusternetworkpolicy.yaml` and `PROFILES.md` exist on `main` today.

| Path | Role |
|---|---|
| `platform/oam/definitions/components/dark-factory.cue` | The component: per-namespace resources, `process` schema, step resolution, preset expansion, policy validation, rendered process |
| `gitops/addons/charts/oam-agent-components/templates/dark-factory.yaml` | Generated by `platform/oam/generate.sh`. **Checked in**; the chart cannot register the type without it |
| `docs/dark-factory/STEP-CONTRACT.md` | The published ABI (§3.1), versioned independently — what a team builds against |
| `gitops/addons/charts/dark-factory-templates/` → `onboarding.factories` | One entry per factory (namespace, repos, installation ID). Renders the namespace, team Role, Application admission policy, ResourceQuota and the D15 External Secrets objects (D15, D20) |
| `gitops/addons/charts/dark-factory-templates/` | Platform step templates (each pinning its image digest), the step runner, the registry ConfigMap, the contract, `factoryPolicy` |
| `examples/dark-factory/platform-steps/` | One image per platform step, built from the chart's scripts (D10) |
| `examples/dark-factory/team-step/` | Reference team step image and its conformance test — what a team copies to start |
| `platform/oam/examples/example-dark-factory.yaml` | Example: preset form, plus a team step |
| `gitops/addons/registry/sandbox.yaml` → `dark-factory-templates` | Registers the chart and the step-image registry prefix for this hub |
| `examples/dark-factory/steps/` | Monolithic steps image and `holdout-gate.sh`. **Superseded by D10's per-step images** |
| `gitops/addons/charts/agent-sandbox/templates/31-clusternetworkpolicy.yaml` | Admin-tier policy keyed on `role in (coder, step)`, distinct priority (D13) |
| `gitops/addons/charts/dark-factory/scripts/`, `holdout/evaluate.js` | Source the platform step images are built from, until the hub factory moves onto them (§8 item 4) |
| `docs/dark-factory/PROFILES.md` | Why there are no language profiles, and why process presets differ (D6) |
