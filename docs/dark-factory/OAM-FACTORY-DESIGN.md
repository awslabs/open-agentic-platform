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
guardrails; and the per-namespace factory — identities, four scoped tokens, warm pool,
holdout source, settings, webhook trigger, status. The contract is **substrate-neutral**:
a factory runs on whichever agent sandbox substrate the cluster provides (Kata or Nitro),
and Lambda MicroVM is sequencing, not a non-goal (§3.2).

**Not building**

| | Why / instead |
|---|---|
| The `shared/` registry tier | Teams reference their own steps directly; promotion needs a review workflow and an owner (D3) |
| Signature verification of step images | Digest pinning and an allowed-registry list ship now; cosign is a follow-up |
| A warm pool for step execution | Steps run as agent sandboxes on demand; pool once real durations are known (D4) |
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
      │   claim SandboxWarmPool dark-factory-coder ─► sandbox (coder)       │
      │   mount Secret    dark-factory-holdout  (hidden scenarios)         │
      │   TEAM STEPS ─► sandbox, no tokens, declared egress only           │
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
| `DF_RESULT` | Path the step must write its result to |

Out — one JSON file at `$DF_RESULT`:

```json
{"status": "success|failure|skipped", "summary": "one line", "details": "optional markdown"}
```

A non-zero exit with no result file is a `failure` carrying the last log line. The result
file is capped at 4 KB (the termination-message limit, D4); `details` beyond that is
truncated and the full log is linked from the status.

A step **never posts its own status**: the runner reads the result and the platform posts
it with the orchestrator token. What makes a status a verdict is **who posted it**, not its
name: `platform/merge` accepts a required `dark-factory/*` status only if its `creator.id`
is the orchestrator account (resolved once with `GET /user`). A same-named status from any
other token counts as missing, which means a refusal. This requires the orchestrator token
to belong to an account no other factory token or `stepSecrets` key belongs to — a
dedicated machine user (D15).

**Capabilities**, requested in the Application and granted narrowly: `workspace: true`
(the branch at `$DF_WORKSPACE`, discarded after), `egress: [<host>...]` (reached **through
the platform egress proxy**, which admits only the declared hosts for that step, identified
by its pod IP; the step's NetworkPolicy allows DNS and the proxy, nothing else), `secrets: [<name>...]` (keys from the factory's
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

**Substrate.** The process depends only on the Sandbox CRD surface: `dark-factory-coder`
points at a SandboxTemplate, and the template is where the substrate lives (each substrate's
SandboxTemplate carries its own runtime settings; Kata, for example, sets `runtimeClassName`,
and the Lambda shim emits the same kind without one). Any substrate **must**
stamp `agent-sandbox.io/role` (`coder` or `step`) — without it the pod silently loses D13's
isolation — and `agent-sandbox.io/substrate`. The substrate is chosen by a `substrate` key
in `dark-factory-config`, never by a template name, so adding one does not fork platform step
versions (D9). Claim and release are the one substrate-specific part (Lambda needs suspend
and resume); they belong in a per-substrate step that `platform/code` calls.

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
| `platform/approval` | Ends the run until a qualifying GitHub review arrives, then starts the continuation run (D8) | `count`, `approvers` | `dark-factory/approval` |
| `platform/merge` | Re-reads the PR head, requires every rendered status and every `requiredSteps` entry from the orchestrator, merges, closes the issue (D7) | none | none (merges) |

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
| `coder.warmPoolSize` | int | `1` | — |
| `coder.runTimeoutMinutes` | int | `30` | `run-timeout-seconds` |
| `claimTtlSeconds` | int | `10800` | `claim-ttl-seconds` |
| `webhook.host` | string | `ingressDomain` | — (D19) |
| `secrets.keyPrefix` | string | `<cluster>/dark-factory/<ns>/<name>` | — (D15) |
| `holdout.requireScenarios` | bool | `true` | — (D17) |
| `eventBus.create` | bool | `true` | — (D14) |

A step's own settings go in its `config` and reach it as `DF_CONFIG` (§3.1).
Platform-supplied values (account, region, Bifrost) are not properties (D19).

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
3. **Isolated execution.** An agent sandbox (Kata or Nitro), default-deny egress, no control-plane reach
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
(`df-step-runner-v1`) as an agent sandbox in the factory namespace with
`automountServiceAccountToken: false`, the token-less executor identity, read-only root,
all capabilities dropped, and default-deny egress plus whatever `egress:` named.

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
| Launch | The runner (a platform Argo task, SA `dark-factory`) creates the step pod directly: `automountServiceAccountToken: false`, SA `dark-factory-executor`, label `role: step` |
| Result | `terminationMessagePath: $DF_RESULT`; the runner reads it from pod status. Needs no token and no API reach from the step |
| Workspace | An init container clones with the factory's **reader** token (contents:read only, D15), mounted only into that init container, so the step's container never sees it. It grants nothing beyond read access to source the step already receives |
| Logs | Read by the runner, linked from the status's target URL |

**Costs.** Per-step sandbox boot latency, paid in parallel across a stage but still paid.
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
- **Verdicts are attributed, not named.** Platform steps own `dark-factory/<name>`, team
  steps `dark-factory/team/<step-name>`; the runner picks the name and the orchestrator
  posts it. Merge checks the poster (§3.1), so naming a status
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
and every team step automatically. The runner stamps `role: step` on each step pod, so
coverage does not depend on the image cooperating. A per-factory policy would mean a tenant
Application owning cluster-wide admin-tier network policy.

The component also keeps a namespaced default-deny egress policy as defence in depth.

**Hostname egress goes through a proxy.** IP-based NetworkPolicy cannot express hostnames,
and hostname-aware policy is not available on the self-managed Karpenter node pools the sandboxes run on. A
platform-owned forward proxy (one per hub) enforces per-step host lists from the rendered
process; the proxy, not the step, is what `allowedEgressHosts` binds.

**Cost:** a proxy to run, and a step that ignores `HTTPS_PROXY` simply has no egress.

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

### D15. Four tokens per factory, from Secrets Manager

ExternalSecrets from the `aws-secrets-manager` ClusterSecretStore at
`<cluster>/dark-factory/<namespace>/<name>/github/{coder,orchestrator,events,reader}`:

| Token | Used by | Scope |
|---|---|---|
| coder | The coder VM (0400, never in env) | Issues read; contents and PRs write. **No commit statuses** — `platform/code` posts for it. Contents:write also opens the merge endpoint on a fine-grained PAT, so **only the ruleset stops a self-merge** (D20) |
| orchestrator | **Platform steps only.** Owned by a dedicated machine user | Adds commit statuses, Issues write and merge. The only account whose statuses merge accepts (§3.1) |
| events | EventSource only | Webhook registration and HMAC |
| reader | D4 workspace init containers only | Contents read. Nothing else |

The orchestrator's account must hold no other token in the factory and must not be one a
team can mint tokens as; otherwise the §3.1 creator check cannot tell the two apart.
Short-lived per-run App tokens (§8 item 6) would replace the standing coder token and
remove its merge reach.

Team steps get none of these (D1); one needing its own credential declares `secrets:` and
gets keys from the factory's `stepSecrets/` prefix, which the team controls and which never
holds a GitHub token we issued. The most exposed components hold the least privilege, and
the cluster prefix lets each cluster's External Secrets identity be limited to its own
paths.

### D16. No AWS identity anywhere in a factory

No factory ServiceAccount carries IAM, and `factoryPolicy` forbids attaching an identity
trait to one. The one GitHub credential a team step's pod ever sees is the D4 reader token:
read-only, and confined to the init container.
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
the runner's result and posts it. Coder output never shares a pod with a credential or with
another repo's scenarios.

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
`ingressDomain`) and into CUE defaults. Bifrost is reached by **ClusterIP** where the
substrate's guests cannot resolve cluster DNS (Kata today) and admitted by a `/32` egress rule, because VPC-CNI matches
egress on the pre-DNAT ClusterIP. The Bifrost address is a per-substrate value: a substrate
whose guests cannot resolve cluster DNS takes the ClusterIP; a substrate on ordinary node pods takes the service name. `webhook.host` defaults to
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
four tokens, the holdout source per repo, warm pool readiness, webhook registration, step
images resolvable), and which `factoryPolicy` applied. The platform also takes over
namespace creation, `webhook.host` defaulting (D19), and the holdout source as a
secrets-store reference (D17); the team keeps what lives in GitHub — the Apps, branch
protection — and its own step images.

**The current `healthPolicy` must be replaced, not extended:** copied from a
Deployment-shaped component, it reads
`context.output.status.readyReplicas >= context.output.spec.replicas` against a
`SandboxWarmPool`, which has no such fields, so it can only ever report the `| false`
fallback.

**Onboarding and tenancy.** The namespace is the tenant boundary, so the team must not
administer it. The platform's onboarding creates `dark-factory-<team>` with:

- a team Role that may apply and read the `Application`, and read Workflows, pods and logs —
  **no** Secrets, Pods, Workflows or RBAC writes;
- a Kyverno policy (Kyverno ships in prod) denying any pod that mounts
  `dark-factory-github-*` unless it comes from a platform template;
- a ResourceQuota sized for the warm pool plus `maxStepsPerStage` step pods.

**Branch protection is checked, not assumed.** At startup and before every merge, the
factory reads the repo's rulesets with the orchestrator token and reports in status whether
the default branch requires every `dark-factory/*` check. A repo without it fails closed:
runs start, merge refuses, and status names the missing rule.

**Cost:** status needs state from objects the component does not own, so `customStatus`
expressions get long and brittle. A reconciler would be better (§8).

### D21. Smaller choices

| Choice | Reason |
|---|---|
| Run key is `<owner>-<repo>-<issue-number>` | The number is in every payload, including a revision run resolved from a PR, which `issue.id` is not. The bare number is unique only per repo, and a factory watches several, so `acme/api#7` and `acme/web#7` would share a mutex and a claim. The key is lowercased and, past 63 characters, truncated with an 8-character hash suffix. |
| `synchronization.mutexes` (list) | Argo v4 removed `mutex`. Verified (§7 evidence): v4.1.4 rejects it; the list form works on v3.6+. |
| No issue title in the claim | Raw issue text broke the claim manifest on quotes. The coder reads the title from the issue it fetches. |
| Revision notes base64-encoded | Arbitrary markdown cannot break the claim manifest. |
| Deterministic Workflow names per event | GitHub redelivers webhooks; duplicates collide (`AlreadyExists`) instead of racing. |
| Registry gate `enable_dark_factory` | One label already turns on the Dark Factory stack; the step templates join it. |

---

## 5. Security model

| Threat | Control |
|---|---|
| Coder code reaches credentials | Agent sandbox (own kernel); no Kubernetes token, no IAM; only a narrow GitHub token at `/etc/secrets` (0400) |
| **Team step reaches credentials** | **No tokens mounted (D1, D15); only declared `stepSecrets` keys, which the team owns** |
| Coder or team step reaches the control plane | Admin-tier ClusterNetworkPolicy on `role in (coder, step)` (D13); default-deny egress plus declared hosts |
| **Team step forges another step's verdict** | **Steps never post statuses; the platform runner reads `$DF_RESULT` and posts under a tier-namespaced context it chooses (D2, D7)** |
| Another token posts a same-named passing status | Merge accepts only statuses created by the orchestrator account; the coder token has no statuses permission (§3.1, D15) |
| **Team step satisfies a required gate with a stub** | **`requiredSteps` may only name `platform/` steps — structural, not configurable (D7)** |
| **Hostile step image** | **Digest-pinned from an allowed registry (D7); agent sandbox, no tokens, read-only root, caps dropped, deny-all egress (D4). Worst case: that team's own PRs fail to merge** |
| Coder games its own tests | Hidden scenarios no step but `platform/holdout` can reach (D17); a judge from a different model family; fail-closed on missing scenarios |
| Coder output executed on the hub (holdout) | Runs through the D4 runner: no token, one repo's scenarios, agent sandbox (D17); `npm install --ignore-scripts`; no AWS identity in the namespace (D16) |
| Unreviewed or failing code merged | Merge only on a human approval event; `platform/merge` refuses unless every required status is green at the PR head; a missing status is a refusal |
| Tampered platform step code | Each platform step's image is digest-pinned in a cluster-scoped, platform-owned template (D10); only the platform changes the digest, through a reviewed commit; each run records the digest it ran (D9) |
| Tenant process drops a required gate | Three overlapping enforcement points (D7) |
| Runaway revision loop | One iteration budget shared by `onFail: revise` and comments (D8); factory-authored comments filtered at the Sensor |
| Two runs for one issue holding two VMs | Mutex and claim keyed on owner, repo and issue number (D21); `maxConcurrentRuns` semaphore (D8) |
| A step reaches another factory | Step egress is DNS plus the proxy only; the proxy admits only the step's declared external hosts, never cluster or pod CIDRs (D13) |
| Cross-factory interference | One factory per namespace (D12); per-namespace events and secrets (D14, D15); Argo locks are namespace-scoped |

**Residual risks**

- Branch protection is the only hard stop on self-merge, and is unavailable on private
  repos on GitHub's free plan.
- **GitHub enforces names, not posters.** A ruleset can pin a required check to an App, not
  to the account behind a PAT. `platform/merge` refuses a forged status, but a human merging
  in the UI relies on the ruleset, which checks only the name. Exploiting this needs a token
  with statuses permission on the repo, which no factory grants except the orchestrator's.
- **Render-time policy binds only the component path.** Team RBAC removes raw Workflow
  submission for the team (D20); a cluster admin can still bypass it.
  `templateReferencing: Strict` would close it cluster-wide (§8).
- **A valid process can still be a bad process**, and team steps widen this: a team can
  compose something that passes policy and verifies very little, or whose own steps always
  return success. Policy floors *our* gates, not the quality of a team's.
- **A team step can exfiltrate the source it is given.** With `workspace: true` and any
  `egress:` host, a step sees the branch and can post it outward. Inherent in letting teams
  run code against their own source, which is why `allowedEgressHosts` is per-environment
  policy rather than a team setting.
- The coder's token is a standing credential; short-lived per-run App tokens would be
  stronger.

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
allows) and enable the ruleset status reports as missing, requiring the `dark-factory/*`
checks (D20). Seed the
holdout scenarios and the four tokens at the paths status reports as missing. The DevOps
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
  them, and `claimTtlSeconds` reaps any VM the deletion misses.
- **Webhooks** registered by the EventSource are deregistered on its deletion; status
  reports any it could not remove.
- **Tokens in Secrets Manager are not deleted.** The platform does not own their lifecycle;
  the team revokes the PATs and removes the `<prefix>/github/*` entries.
- **Open `df/` PRs and branches stay open**, so work in progress is not lost silently; their
  `dark-factory/*` statuses can no longer turn green.

The namespace itself is removed through onboarding (D20), not the Application.

---

## 7. Validation

**Evidence pinned so far**

| Claim | Command | Cluster / version | Date |
|---|---|---|---|
| `argo-cluster-role` grants configmaps get/list/watch (D11) | `kubectl get clusterrole argo-cluster-role -o yaml` | dora-showcase, Argo `<fill>` | `<fill>` |
| `mutex` rejected, `mutexes` accepted (D21) | `argo lint` | Argo v4.1.4 | `<fill>` |
| Server-side dry run of the component's namespaced objects | `kubectl apply --dry-run=server` | dora-showcase | `<fill>` |

Already established, on the substrate rather than this design's shape: `mutexes` accepted
and `mutex` rejected on Argo v4.1.4; a server-side dry run on `dora-showcase` for the
component's namespaced objects (Sandbox and ExternalSecret CRDs are absent there); and the
holdout evaluator against sample scenarios with a stub judge that always says yes — an
honest implementation passed 4/4, a hard-coded one failed 2/4, to be re-run in the
`platform/holdout` image (D10).

Needed, none of it done:

- **Go/no-go: `configMapKeyRef` defaults resolve in the Workflow's namespace** for a
  Workflow submitted against the namespaced `dark-factory-process` whose tasks are
  `templateRef` with `clusterScope: true`. Owner: `<fill>`. Gates any factory beyond the
  hub. If it fails, the fallback is for the component to render settings into
  `dark-factory-process` as literal parameter defaults, which it already renders per factory
  (D5).
- **A conformance suite for the step contract** — a reference step in two languages, plus
  adversarial ones: no result file, malformed JSON, non-zero exit, a result claiming
  `success` after an internal error, a step attempting to post a GitHub status, a step
  reaching for the API server and the holdout Secret. The last three must fail closed. This
  suite is the executable form of D1's three invariants. Also: a passing `dark-factory/*`
  status posted by a non-orchestrator token must be refused by `platform/merge` (§3.1).
- CUE type-checks: each preset, `addSteps`, an explicit process, a violation of each
  `factoryPolicy` rule (must fail naming the rule), a tag-not-digest image, and a
  `requiredSteps` entry naming a team step (must be rejected).
- A rendering golden test per preset: stage order, step set, and the required-check list
  `platform/merge` receives.
- Per-platform-step tests against the contract, each starting with empty `DF_PR_NUMBER`.
- `helm lint` and `argo lint` on every platform step template and the runner; every
  `configMapKeyRef` key present in the rendered `dark-factory-config`.
- Both Admin ClusterNetworkPolicies admitted with distinct priorities on a live VPC-CNI, and
  that `role=step` covers runner-launched sandboxes on each substrate (D13).
- Sandbox cold-boot latency per substrate for a trivial step image, to decide whether §2's "no warm pool"
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
6. **Short-lived GitHub App tokens** per run instead of standing tokens.
7. **Update `setup-secrets.sh`** for per-factory paths, the holdout source and `stepSecrets`.
8. **Implement D10:** split the chart's scripts into one image per platform step, each
   honoring the contract; pin each by digest in its template; stand up the build-and-publish
   pipeline; delete `examples/dark-factory/steps/` and `stepsImage`.
9. **A reconciler for status** instead of long `customStatus` expressions (D20).
10. **Optional: verdicts from a platform GitHub App**, posted as check-runs, with the
    ruleset pinned to that App, so GitHub itself enforces who posted (§5).

---

## 9. Open questions

1. **Reserve platform templates for security controls only.** The direction D1 implies:
   keep shrinking the platform-owned set until a step is reserved *only* because a team
   implementation could weaken a security control, never because it could merely be wrong.
   `merge`, `security-scan` and `approval` are the boundary and stay. `changeset` can move
   once the platform evaluates skip decisions from a team-supplied path list rather than
   trusting a team step's boolean — then a wrong changeset is a wrong input, not a skipped
   gate. `holdout` splits: the scenario mount stays platform-only, but the evaluator could
   be a team step given scenarios it cannot exfiltrate (D17). `platform/code` is not
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

Planned layout. Files already on this branch are drafts and will change; only the chart
sources, `31-clusternetworkpolicy.yaml` and `PROFILES.md` exist on `main` today.

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
| `gitops/addons/charts/dark-factory/scripts/`, `holdout/evaluate.js` | Source the platform step images are built from, until the hub factory moves onto them (§8 item 4) |
| `docs/dark-factory/PROFILES.md` | Why there are no language profiles, and why process presets differ (D6) |
