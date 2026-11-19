# Design: Dark Factories as OAM components

| | |
|---|---|
| Status | Proposed; implementation in progress |
| Components | `dark-factory` OAM component, `dark-factory-templates` platform chart |

---

## 1. Summary

A **Dark Factory** takes a GitHub issue in and produces a reviewed, gated PR out. A team
gets one by applying a KubeVela `Application` that describes **its own delivery process**,
assembled from the platform's step catalog:

```yaml
apiVersion: core.oam.dev/v1beta1
kind: Application
metadata: {name: acme-factory, namespace: dark-factory-acme}
spec:
  components:
    - name: acme-factory
      type: dark-factory
      properties:
        repositories: {owner: acme, names: [api]}
        process:
          stages:
            - {name: build,  steps: [{use: platform/code}]}
            - name: verify
              steps:
                - {use: platform/holdout, config: {threshold: 0.9}}
                - {use: platform/ci-check, config: {check: "ci/contract-tests"}}
                - {use: platform/security-scan}
              onFail: revise
            - {name: review, steps: [{use: platform/approval}]}
            - {name: release, steps: [{use: platform/merge}]}
```

Teams do not share one SDLC. They differ on which checks gate a change, in what order,
where humans approve and what happens when a check fails — so the team owns the
**process**, and the platform owns the **steps** it is built from and the floor no process
may go below:

| Layer | Owner |
|---|---|
| **The merge decision** — whether a PR may merge, and which statuses are required | **Platform.** Not authorable by a team. |
| **Isolation** — where code runs, what it reaches, what credentials exist | **Platform.** |
| **Guardrails** — minimums no process may go below, per environment | **Platform** (`factoryPolicy`, D7) |
| **Steps** — what each step does | **Platform** in v1: the catalog in §3.3. A team brings its own tooling into the process through `platform/ci-check`, which gates on any GitHub check the team already runs (D18) |
| **Process** — stages, order, approvals, failure handling | **The team** (`process`, D5–D8) |

It is tempting to reserve more — to fix the pipeline's shape and call it a security control.
It is not: a fixed shape is bypassable by any namespace admin who can create a Workflow, so
the merge decision and the GitHub ruleset are the real controls (D7, D20), and the shape can
belong to the team.

**Team-authored step images are a later version, not v1** (§8). They would let a team run its
own code as a step inside the factory, which needs a published step ABI, a registry, a runner
that launches arbitrary images in sandboxes, and per-step capabilities and egress — most of
the surface this design would otherwise have to maintain before a single platform step has
run end to end. v1's `use:` field already addresses steps as `<tier>/<name>`, so
`use: acme/...` can be added later without changing the developer API; v1 rejects any `use:`
outside `platform/` at render.

---

## 2. Scope

The coder runs model-written code from public issue text, and holdout runs that code's
tests. Every decision below keeps both away from credentials, the merge decision, and the
cluster control plane.

**Building:** platform steps `code`, `changeset`, `ci-check`, `holdout`, `security-scan`,
`approval`, `merge`, as images honoring one internal step contract (§3.1); a `process`
schema with presets and explicit stages (`onFail`, `skipWhen`); `factoryPolicy` guardrails;
and the per-namespace factory — identities, four short-lived tokens minted from the platform
GitHub App, sandboxes, holdout source, settings, webhook trigger, status. Sandboxes are
rendered by #151's `type: sandbox` and nothing else (§3.2), so a factory runs on whichever
runtime the **cluster** is configured for — Nitro by default, Kata by annotation — and never
chooses one itself. Lambda MicroVM is sequencing, not a non-goal (§3.2).

**v1 is GitHub only.** Intake, verdicts, tokens and the merge barrier are all GitHub. A second
forge (GitLab, for example) would plug in at four seams, and the design keeps GitHub out of
everything else so it does not leak into every step:

| Seam | GitHub in v1 | Where |
|---|---|---|
| Intake | Issue-labeled webhook into the EventSource | D14 |
| Verdict posting | Check-runs from the platform GitHub App, read back by `platform/merge` | §3.1, D7 |
| Token minting | `GithubAccessToken` generator → `PushSecret` | D15 |
| Merge barrier | Ruleset requiring the App's checks, verified at startup and before merge | D20 |

Steps talk to the forge only through the platform's verdict-posting and token paths, never
directly in their own logic beyond reading the PR, so a second forge is new implementations
of these four seams rather than a change to every step.

**Not building**

| | Why / instead |
|---|---|
| Team-authored step images, the step registry and the `shared/` tier | A later version, once the platform steps run end to end (§1, §8) |
| Signature verification of images | Digest pinning and an allowed-registry list ship now; cosign is a follow-up |
| Forges other than GitHub | v1 is GitHub only; the four seams above are where another plugs in |
| `deploy`, environment promotion | Nothing needs it before merge gating works |
| Pluggable intake | `issue-labeled` with a configurable label; issue types and slash commands are additive |
| Autonomy 3.5 (low-risk auto-merge) | Targets the README's Level 3; needs `changeset` proven first |
| Per-host egress for holdout's install launch | EKS enforces `domainNames` rules only on Auto Mode nodes, and Kata pools are self-managed; v1 egress is public-or-none, with per-host egress an option once §7 confirms it for Nitro guests (D13) |
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
       │ STEP RUNNER   df-step-runner-v1  ← starts holdout's sandboxed runs │
       │ GUARDRAILS    factoryPolicy (chart values, per environment)        │
       │ ClusterNetworkPolicy   deny coder + holdout launches → control plane│
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
      │   launch holdout install/test ─► sandboxes from type: sandbox      │
      │   mount Secret    dark-factory-holdout  (test launch only)         │
      │ SANDBOXES  type: sandbox, platform-owned Application (onboarding)  │
      └────────────────────────────────────────────────────────────────────┘
```

### 3.1 The step contract

Every platform step is an OCI image (D10) whose entrypoint reads the environment and
writes one result file. In v1 this is an **internal contract**: one shape for every
platform step, so steps are tested the same way and the renderer needs no per-step
knowledge. It is written to become the published ABI when team-authored steps arrive (§8),
which is why it is specified here rather than left to each template.

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
  plus the log's tail (D8). There is no size limit and no format. **One exception:** holdout's test launch runs coder-written code with the
  scenarios mounted, so its log and `summary` are never linked, posted or forwarded to the
  coder — `platform/holdout` posts only the judge's counts, as the current chart does (D17).

The verdict travels as the pod's termination message (D4), whose 4 KB limit a two-field
verdict never approaches. A non-zero exit with no result file is a `failure` carrying the
last log line, and a result the platform cannot parse — malformed, or truncated because a step
wrote more than the limit (the kubelet keeps the *last* 4096 bytes, so `status` would be
lost) — is a `failure`, never a partial pass.

A step's logic **never posts its own verdict**: its template reads `$DF_RESULT` and posts it
as a **check-run from the platform GitHub App** (D15), so posting is one piece of platform
code rather than something each step reimplements — and the forge seam (§2) is in one place. What makes a check a verdict is
**which App created it**, not its name:

- `platform/merge` accepts a required `dark-factory/*` check only if its `app.id` is the
  platform App's. A same-named commit status, or a check-run from any other App, counts as
  missing, which means a refusal.
- The branch ruleset requires each `dark-factory/*` check **from the platform App**
  (rulesets can pin a required check to an App), so GitHub enforces the same rule when a
  human merges in the UI.
- Only the orchestrator token carries `checks: write`. The coder, reader and events tokens
  are minted from the same App without it, so nothing else in a factory can create a
  platform check.

**Code that is not platform code** runs in exactly two places, both sandboxed: the coder,
and holdout's two launches, which run the coder's output (D17). Neither can reach the
orchestrator token, a Kubernetes token, AWS credentials or merge authority — those are
absences, not parameters (D4, D16). The holdout launches get a workspace on a per-launch
volume deleted with the run (D4); the install launch gets public HTTPS only where
`factoryPolicy.stepEgress` is `public` (D7, D13), and the test launch gets no network at all
(D13, D17).

### 3.2 The per-namespace contract

Steps resolve everything by fixed name in the Workflow's own namespace. The team's
`dark-factory` component creates the process and its plumbing; onboarding (D20) creates the
credentials and the sandboxes. Names are defined once in `dark-factory-templates/values.yaml`
→ `contract`.

| Name | Kind | Created by | Purpose |
|---|---|---|---|
| `dark-factory` | ServiceAccount | Component | Workflow identity: claims the coder sandbox, launches holdout, submits revision runs |
| `dark-factory-executor` | ServiceAccount | Onboarding (`type: sandbox`) | Token-less identity of the holdout launches |
| `dark-factory-clone` | ServiceAccount | Component | Token-less identity for D4's workspace clone pods |
| `dark-factory-sandboxes` | Application (platform-owned) | Onboarding | Three `type: sandbox` components, below |
| `dark-factory-coder` | `type: sandbox` component | Onboarding | The pool `platform/code` claims |
| `dark-factory-holdout-install`, `dark-factory-holdout-test` | `type: sandbox` components | Onboarding | The SandboxTemplates holdout's two launches are instantiated from (D4, D17) |
| `dark-factory-github-<role>` | Secret (`token`) ×4 | Onboarding, empty; filled by D15's push | GitHub tokens. The orchestrator token is for **platform steps only** |
| `dark-factory-config` | ConfigMap, label `workflows.argoproj.io/configmap-type=Parameter` | Component | Factory-wide settings (D11) |
| `dark-factory-holdout` | Secret, optional | Component (ExternalSecret) | Hidden scenarios, keys `<owner>-<repo>.json` (D17) |
| `dark-factory-process` | WorkflowTemplate | Component | The rendered process (D5) |

**Sandboxes come from #151 and nothing else.** One definition owns how a sandbox is
rendered: #151's `type: sandbox`. The factory never renders a SandboxTemplate, pool or
sandbox pod spec of its own, so a change to #151 reaches every factory with no edit here.
Onboarding creates, in each factory namespace, a **platform-owned Application**,
`dark-factory-sandboxes`, applied by the platform's GitOps identity rather than the team, with
three `type: sandbox` components:

| Component | Image | Settings beyond #151's defaults |
|---|---|---|
| `dark-factory-coder` | The coder image from the onboarding entry, digest-pinned from `allowedRegistries` (D7) | `role: coder` (§3.5); `mode: isolated` (the coder flow needs `exec`, which `confidential` denies); `service: false`; `warmPool` from the onboarding entry, default `0` (§3.4); Nitro throttles where the entry sets them |
| `dark-factory-holdout-install` | The platform holdout test image (D10) | `role: step` (§3.5); `service: false`; `warmPool: 0` |
| `dark-factory-holdout-test` | The same image | `role: step` (§3.5); `service: false` — `type: sandbox` defaults `service: true` and rejects it with `networkIsolated`; `warmPool: 0`; `networkIsolated: true` when the cluster's class is a Nitro class |

Everything else is #151's, unchanged: the runtime class from the cluster secret (Nitro by
default; a Kata cluster sets `sandbox_runtime_class: kata-clh` and
`confidential_sandbox_runtime_class: none` through `<hub>/config`, #151
`registry/agentcore.yaml:82-84`), the render guards, `runtimeClassName` as the only scheduling
field, the pod annotations, `envVarsInjectionPolicy`, NetworkPolicy management, DNS policy,
pool health and the card. No team Application property reaches any of it, and there is no
`substrate` key. Because the sandboxes are platform components, the coder image, warm-pool
size and throttles are onboarding settings, not team properties (§3.4).

This depends on **one upgrade to #151's `type: sandbox`**, a platform-settable role,
specified separately in §3.5 so it can be reviewed and landed in #151 on its own.

**How the factory uses them.**

- **Coder.** `platform/code` claims from `dark-factory-coder` and sets `lifecycle.shutdownTime`
  — the run timeout plus a margin — with `shutdownPolicy: Delete`, since nothing else reaps a
  sandbox whose caller died (#151 removed the card's `idleTimeout`/`maxLifetime`). The claim
  must not repeat a variable the template already sets, such as `BIFROST_URL`, or operator
  v0.5.1 rejects it.
- **Holdout launches.** A claim cannot carry a per-run volume, so the runner instantiates the
  holdout templates instead: it creates a `Sandbox` whose pod template is the one #151
  rendered into `dark-factory-holdout-install` or `-test`, read at run time, and adds **only**
  run-specific fields — the workspace PVC (read-only for the test launch), this repo's
  scenario key (test launch only), `restartPolicy: Never`, `spec.shutdownTime`, and the
  `dark-factory.io/launch` label. It never sets or changes an isolation field, so #151's
  guards, which ran on the template, still hold (D4).

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
| `platform/holdout` | Runs the hidden scenarios against the branch in two sandboxed launches (install, then test), then has a judge confirm them; fails closed if the repo has no scenarios (D17) | `threshold`, `requireScenarios`, `judgeModel`, `judgeRuns`, `judgeQuorum` | `dark-factory/holdout` |
| `platform/security-scan` | Waits for the Security Agent App's review and copies its verdict, failing at or above `blockLevel` (D16) | `blockLevel`, `waitSeconds` | `dark-factory/security-scan` |
| `platform/approval` | Ends the run until a review arrives, then starts the continuation run; passes when the PR's GitHub `reviewDecision` is `APPROVED` at the head commit (D8, D20) | none | `dark-factory/approval` |
| `platform/merge` | Re-reads the PR head, requires a platform-App check for every rendered step and every `requiredSteps` entry, merges, closes the issue (D7) | none | none (merges) |

### 3.4 Component properties

| Property | Type | Default | Config key |
|---|---|---|---|
| `repositories` | `{owner, names}` — **one owner** | required | — (a factory covers one GitHub App installation, D15) |
| `triggerLabel` | string | `dark-factory` | — |
| `catalogVersion` | string | `v1` | — (D9) |
| `process.preset` | string | `standard` | — (D6) |
| `process.stages` | `[{name, steps, onFail, skipWhen}]` | from preset | — |
| `process.addSteps` | map of stage → steps | `{}` | — |
| `process.approvals` | int | `1` | — (D7) |
| `process.maxIterations` | int | `3` | `max-iterations` |
| `maxConcurrentRuns` | int | `2` | `max-concurrent-runs` |
| `coder.engine` | `claude` \| `kiro` | `claude` | `coder-engine` |
| `coder.runTimeoutMinutes` | int | `30` | `run-timeout-seconds` |
| `webhook.host` | string | `ingressDomain` | — (D19) |
| `holdout.requireScenarios` | bool | `true` | — (D17) |
| `eventBus.create` | bool | `true` | — (D14) |

A step's own settings go in its `config` and reach it as `DF_CONFIG` (§3.1).
Platform-supplied values (account, region, Bifrost, the runtime class) are not properties
(D19, §3.2). The sandboxes' settings — coder image, warm-pool size, Nitro throttles — are
not team properties either: they belong to the platform-owned `dark-factory-sandboxes`
Application, set per factory in its onboarding entry (§3.2, D20). The coder image there is
digest-pinned from `allowedRegistries` (D7), because it runs with the coder token mounted. GitHub App installation IDs are not properties either: they live in the
platform's onboarding record (D15). Nor is the secrets-store prefix: it is derived in CUE as
`<cluster>/dark-factory/<ns>/<name>` with no override, because External Secrets' identity is
per cluster and a settable prefix would let a team sync another factory's holdout file or
HMAC secret into its own namespace.

**Why the coder pool's size defaults to 0.** The shipped claim injects env
(`20-workflowtemplate-df-run.yaml:226`), and operator v0.5.1 bypasses the warm pool for any
claim carrying env (`sandboxclaim_controller.go:1626-1627`), so a warm member is never
adopted — and on Nitro it holds a sandbox node while idle. Delivering run inputs without
claim env, so the pool becomes useful, is a follow-up (§8).


### 3.5 Upgrade required in #151: a platform-settable role

The factory renders its sandboxes with #151's `type: sandbox` (§3.2), and that needs one
upgrade to #151. It is the only change outside this design, and the factory cannot ship
without it.

**Why.** `sandbox.cue` (#151 at `90ae55b6`, lines 334–342) hard-codes the pod label
`agent-sandbox.io/role: "session"`, and `type: sandbox` has no parameter that sets a pod
label. #151 chose `session` deliberately: `role: coder` selects the Dark Factory coder egress
regime, which would otherwise apply to an unrelated workload. But that same label is how the
factory's isolation finds its pods — D13's Admin policy selects `role in (coder, step, clone)`
— so a factory sandbox rendered as `session` would run with none of D13's rules.

**The upgrade.**

1. **A `role` parameter** on `type: sandbox`: `*"session" | "coder" | "step"`, rendered into
   the pod template's `agent-sandbox.io/role`. The default keeps every existing
   `type: sandbox` user unchanged.
2. **Platform-only use of the factory roles.** Because the role selects network rules, a
   team that could set `role: coder` on its own sandbox could put any workload under the
   factory's regime. A cluster-wide `ValidatingAdmissionPolicy` (`failurePolicy: Fail`, on
   CREATE and UPDATE of Applications) admits a `type: sandbox` component with a role other
   than `session` only from the platform's GitOps identity — the one that applies
   `dark-factory-sandboxes` at onboarding (D20).
3. **A matrix row** in #151's `scripts/test-isolation-matrix.sh`: each role renders the right
   label, and a non-`session` role from any other identity is rejected.

Nothing else in #151 changes; every other setting the factory uses (§3.2's table) is already a
`type: sandbox` parameter.

| Option | Verdict |
|---|---|
| **A `role` parameter, platform-only for non-`session` values** (**chosen**) | Smallest change; explicit; one definition still owns sandbox rendering |
| A generic pod-labels parameter, with D13 keyed on a factory-only label | Also a #151 change, and still needs the same platform-only admission rule; more general than anything needs |
| Copy `sandbox.cue` into the factory | No #151 change, but copies drift — what §3.2 moved away from |
| Make D13 select `session` | Applies the factory's rules to every interactive session in the cluster |

---

## 4. Decisions

### D1. The platform owns the merge decision and the steps; teams own the process

The central decision. A process — which stages exist, their order, where humans approve,
what happens on failure — is not a security control: a namespace admin who can create a
Workflow can bypass any fixed shape, so the merge decision and the ruleset are what hold
(D7, D20). The process therefore belongs to the team. Steps are different: some of them
*are* the controls.

| Step | Why platform-owned | Team-ownable later? |
|---|---|---|
| `merge` | It *is* the merge decision: which statuses are required, and that a missing one is a refusal | **Never** |
| `approval` | Authorization: that a human approved, and from which team | **Never** |
| `holdout` | Confidentiality: the only reader of the hidden scenarios | **Never** (the evaluator, possibly — §8) |
| `security-scan` | Policy may require its verdict | **Never** |
| `changeset` | Feeds `skipWhen` and auto-merge; a wrong answer skips gates | Once the platform evaluates skip decisions from a team-supplied path list (§8) |
| Contract tests, licence scan, compliance tooling, model-graded rubric | Report a verdict; cannot weaken anything else | **Yes, in the later version** (§8). In v1 they run where the team already runs them and gate through `platform/ci-check` (D18) |

**The principle for the reserved set.** A step is reserved *only* because a team
implementation could weaken a security control — never because it could merely be wrong.
`merge`, `approval`, `holdout`'s scenario mount and `security-scan` are that boundary for
good. In v1 every step is platform-built regardless, because team-authored steps are a later
version (§1, §8); the principle decides what that version may open up. The end state is a
platform owning the merge decision, the isolation substrate and the credential boundary.

**Costs.** A team whose check is not already a GitHub check cannot add it to the factory in
v1 except through `platform/ci-check`, so the platform is in the path for any genuinely new
step type. And a team can compose a process that passes policy while verifying very little
(§5).

### D2. One contract for every platform step

| Option | Verdict |
|---|---|
| **Env in, result file out, the same for every step** (**chosen**) | One shape to test, and the renderer needs no per-step knowledge; a new platform step is additive. It is also the ABI team-authored steps would build against later (§8), so it is written as one now |
| Argo parameters per step type | Every step's template becomes its own interface, and the renderer must know each |
| A step SDK or base image | Couples every step to one language |

Platform steps use two things the contract does not give a step's own logic: credentials
mounted by name, and verdict posting, both done by the step's template (§3.1).

**Testing a step.** A local runner, `df-step-test`, ships with the contract: it runs a step
image with a `DF_*` environment and a workspace from a local checkout, then validates the
result file. A **process** change is tested the way any change to a factory is — by running
it on a factory in a dev environment against a scratch repo; there is no separate process
simulator.

**Cost.** Internal for v1, so it can still change cheaply; it becomes expensive to change
only once teams build against it (§8), at which point a breaking change is a *contract*
version, not a step version (D9).

### D3. Step addressing

A step is addressed `<tier>/<name>`, resolved at render. v1 has one tier, `platform/`; a
`use:` naming any other tier fails render. The address shape exists so team (`<team>/<name>`)
and shared (`shared/<name>`) tiers can be added later without changing the developer API
(§8). The platform publishes the catalog of `platform/` steps — name, version, `config` keys,
docs — as the §3.3 table and a ConfigMap status can read.

### D4. Holdout's launches: sandboxes instantiated from #151's templates

Two v1 launches run code that is not platform code: holdout's **install** and **test**
launches, which run the coder's output against the hidden scenarios (D17). The coder itself
is a claimed sandbox (§3.2). The platform-owned **runner** (`df-step-runner-v1`, a
cluster-scoped template) starts the two holdout launches, reads their results and hands them
to `platform/holdout`'s judge.

| Option | Verdict |
|---|---|
| **A `Sandbox` instantiated from a `type: sandbox` template** (**chosen**) | Isolation is #151's, unchanged; the runner adds only run-specific fields (§3.2) |
| Claim from a `type: sandbox` pool | The simplest path, but a claim cannot carry the per-run workspace volume or the per-repo scenario key |
| Hardened ordinary pod | A second isolation posture to maintain, and on EKS VPC-CNI a namespaced NetworkPolicy does not reliably apply to Sandbox-owned pods (D13) |

**Launch shape.** Neither launch runs as an Argo task: Argo's `wait` container needs a
ServiceAccount token, which in the same sandbox would reach the API server and every
`workflowtaskresult` in the namespace. Instead:

| Concern | Mechanism |
|---|---|
| Launch | The runner (a platform Argo task, SA `dark-factory`) creates a **`Sandbox`** whose pod template is the one #151 rendered into `dark-factory-holdout-install` or `-test` — runtime class, `networkIsolated`, ServiceAccount `dark-factory-executor` with no token, `role: step` and every other isolation field come from there, and the runner changes none of them. It adds only the run-specific fields in §3.2: the workspace, the scenario key, `restartPolicy: Never` (or a finished launch restarts and never reaches `Succeeded`), `spec.shutdownTime` and the `dark-factory.io/launch` label |
| Result | `terminationMessagePath: $DF_RESULT`; the runner reads it from the sandbox pod's status. Needs no token and no API reach from the launch. Unparseable means `failure` (§3.1) |
| Workspace | A **per-run PVC** (below). The sandbox mounts it; the launch holds **no** credential and needs **no** network to get its source. Scratch space is #151's `emptyDir` with its `sizeLimit` |
| Logs | The install launch's log is linked from holdout's check-run. The **test launch's log is never surfaced** — not linked, posted or sent to the coder (§3.1, D17) |

**Workspace delivery: a per-run PVC.** The clone cannot happen inside a launch: under
`stepEgress: none` the install launch has DNS only, and the test launch has no network at
all. So for each holdout run, the runner:

1. creates a PVC named from the **Workflow's UID and the holdout node's ID** — unique per run,
   so a later run of the same issue never collides with it or mounts it — with
   `volumeMode: Filesystem` and `accessModes: [ReadWriteOncePod]`, from the platform's
   workspace StorageClass (D19: an EBS gp3 class with `WaitForFirstConsumer`, and on Auto Mode
   `allowedTopologies` for `eks.amazonaws.com/compute-type: auto`, or the PVC is never
   provisioned), sized by `factoryPolicy.workspaceSize`;
2. runs a short **clone pod** (below), which fills the PVC and exits;
3. creates the install launch, then the test launch, each mounting the PVC. The volume's
   topology pins them to the clone pod's availability zone, which the scheduler applies on its
   own;
4. **deletes the PVC** after its last launch, rather than leaving it until the Workflow's TTL.
   The Workflow also owns it, as a backstop.

**The clone pod** is platform code and holds the only credential in the chain:

| Concern | Setting |
|---|---|
| Identity | ServiceAccount `dark-factory-clone`, `automountServiceAccountToken: false` |
| Labels | `agent-sandbox.io/role: clone`; D13's Admin policy gives that label DNS and public TCP 443 and nothing else |
| Credential | The **reader** token (contents:read, D15), mounted as a file and supplied through a git credential helper. It is never written into the remote URL, `.git/config` or anywhere on the volume, so a launch that mounts the volume cannot find it |
| Clone | `--no-recurse-submodules`, `GIT_LFS_SKIP_SMUDGE=1`; the branch only |

An EBS volume attaches to one pod at a time: on Nitro it is attached to the sandbox as a
block device, and AWS documents `ReadWriteOncePod` as the mode to use. The clone pod, the
install launch and the test launch use the volume **one after another**, never at once,
which that allows. Holdout's two launches therefore share one PVC, and the installed tree passes from the install launch to the test launch with no copy;
the test launch mounts it **read-only**, so coder code with the scenarios mounted cannot write
them anywhere a later launch would read (D17). Volumes are never resized in place (Nitro does not support expansion while attached),
so `workspaceSize` is fixed per launch.

What is documented: Nitro Sandboxes mount EBS volumes through the EBS CSI driver in both
volume modes (on Auto Mode, provisioner `ebs.csi.eks.amazonaws.com`), and EFS is unavailable
under `networkIsolated` — which is why the workspace is EBS, not EFS. What is not documented,
and is §7's go/no-go: EBS under `networkIsolated`, and EBS mounts on the Kata pools.

| Option | Verdict |
|---|---|
| **Per-run PVC, cloned by a platform pod** (**chosen**) | Works under `stepEgress: none` and `networkIsolated`; no credential in a launch; Kubernetes-native, deleted with the run |
| Runner streams a tarball over `exec` | No volumes, but the runner needs `pods/exec`, workspaces flow through the API server, and it rules out `confidential` launches permanently |
| Clone inside the launch | Needs GitHub egress in the launch, which `stepEgress: none` and `networkIsolated` forbid |
| An OCI artifact or object store | A store and credentials to run; D16 bars AWS identity in a factory |

**Costs.** Two sandbox boots per holdout run, plus a volume create, a clone pod and three
attaches; a holdout run is limited to one availability zone; and the quota must cover PVC
count and storage (D20).

### D5. The process is rendered to a per-namespace WorkflowTemplate

| Option | Verdict |
|---|---|
| **The component renders `process` into `dark-factory-process`; tasks are `templateRef`s with `clusterScope: true`** (**chosen**) | KubeVela already renders and reconciles it, so a process change is a normal Application update. Readable, diffable in ArgoCD, holds no step code. |
| A cluster-scoped template interpreting the process at runtime | Nothing per factory, but `withParam` fan-out cannot express per-stage `onFail` or heterogeneous dependencies cleanly, debugging becomes "read the interpreter", and the UI shows a generic loop instead of named stages. |
| KubeVela's Application `workflow` field | Runs per deployment of the Application, not per issue. |

A stage renders as DAG tasks depending on the previous stage's; steps within a stage run in
parallel. Every task is a `templateRef` to a platform step at the factory's
`catalogVersion`, so the rendered process holds no step code.

**Costs.** A per-factory object holds the shape — acceptable because it holds no step code,
but a process bug can be unique to one factory. The renderer caps stages and steps per
stage (D7) rather than finding the etcd object limit in production.

### D6. Presets

**Decision:** `process.preset` names a platform-defined process the component expands
before applying overrides. `standard` is the Level 3 process the README describes —
implement, verify (holdout, security-scan, and the DevOps Agent via `platform/ci-check`),
approve, merge — and the recommended starting point. A team may add catalog steps
without abandoning the preset — for example, gating on a check it already runs:

```yaml
process:
  preset: standard
  approvals: 1
  maxIterations: 3
  addSteps:
    verify: [{use: platform/ci-check, config: {check: "ci/contract-tests"}}]
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
  allowedRegistries: ["<acct>.dkr.ecr.<region>.amazonaws.com/"]   # for the coder image (§3.2)
  stepEgress: none          # none | public — holdout's install launch egress (D13, D17)
  workspaceSize: 10Gi       # per-run holdout workspace volume (D4)
  maxStages: 12
  maxStepsPerStage: 8
```

1. **Render.** A non-compliant process fails the Application naming the violated rule, so
   nothing deploys weakened. Covers `requiredSteps`, `minApprovals`, size caps, that every
   `use:` names a `platform/` step, and that `repositories` names one owner. Onboarding
   applies `allowedRegistries` and digest pinning to the coder image it sets (§3.2).
2. **Merge.** `platform/merge` requires the status of every step in the rendered process
   **and** every `requiredSteps` entry, declared or not — so deleting a required step does
   not produce a mergeable PR.
3. **GitHub.** A ruleset requiring the `dark-factory/*` checks from the platform App.

Each covers the others' bypass: render gives the good error message but is only reached
through the component; merge catches a hand-written Workflow that skipped a step; branch
protection catches one that skipped merge.

**Verdicts are attributed, not named.** Platform steps own `dark-factory/<name>`, and the
platform App posts them. Merge and the ruleset both check the App (§3.1), so naming a status
`dark-factory/security-scan` from another token gains nothing. `requiredSteps` names
`platform/` steps by construction; when team tiers arrive (§8) it must stay limited to them,
or a team could satisfy a platform-mandated gate with its own stub.

**Cost.** Policy lives in Helm values, invisible in the tenant's Application, and is
per environment — so one Application can be valid on one cluster and rejected on another.
Intended for dev versus production, but it makes promotion a thing to test.

### D8. Failure handling, fast paths, and one iteration budget

| `onFail` | Behaviour |
|---|---|
| `stop` (default) | The run fails; statuses stay red; the PR is not mergeable |
| `revise` | Failing steps' `summary` and log tail go back to the coder, consuming one iteration |
| `notify` | Post to the PR and continue. For advisory checks |

`revise` treats every step alike: a failed `ci-check` on a team's contract tests feeds the
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

Platform steps are versioned by the platform, as `df-step-<name>-<version>` pinning an image
digest. A factory pins `catalogVersion` (or a single step's `version`) and the component
renders every platform `templateRef` at it, so one run never mixes platform versions. A
compatible fix keeps its version; an incompatible change (new required `config` key, changed
status semantics) ships as a new version of **that step**. The contract itself is versioned
separately (D2).

The version names the **interface**, not the artifact: a compatible fix ships as a new
digest inside the same `df-step-<name>-<version>`, so factories pick it up without bumping
anything. What is immutable is each run: the template pins one digest at any moment, the run
records which, and status reports it (D20).

**Cost:** two things carry versions (contract and platform steps). Status reports the
resolved set (D20).

### D10. Platform steps are images too

Platform steps run logic adapted from the `dark-factory` chart (`merge.js`, `iterate.js`,
`security-wait.js`, and `holdout/evaluate.js`), which that chart mounts from a
ConfigMap — and a ConfigMap can only be **mounted** in its own namespace. (Reading one by
name, which D11 relies on, is a different path: the controller resolves `configMapKeyRef` in
the Workflow's namespace.)

| Option | Verdict |
|---|---|
| **One image per platform step, digest-pinned in its template** (**chosen**) | One execution model, one contract, one way to test — and the shape a team step would have later (§8) |
| Scripts embedded in the templates as Argo raw artifacts | No image build, but it fuses step code into templates, which a later team tier could not share — see below. |
| One image carrying every platform step's scripts | Fewer artifacts, but a change to `merge` re-releases `holdout`, and per-step versioning (D9) stops meaning anything. |

**Why images, when embedding scripts would avoid a build.**

- **It keeps the reserved set movable.** D1's principle lets `changeset` and holdout's
  evaluator become team-ownable later. If platform steps are images honoring the contract,
  that is a catalog change; if they are scripts fused into cluster-scoped templates, it is a
  rewrite.
- **One test path.** `df-step-test` and §7's conformance suite cover every step the same
  way.
- **It drops a pile of incidental mechanism**: no `.Files.Get`, no raw artifacts, no
  symlinking the chart's `scripts/` *and* `holdout/evaluate.js` into the templates chart,
  no confirming ArgoCD follows those symlinks, and no rewriting `jq` in node to suit a
  stock base image. The image installs what it needs.

**Drift is handled by the digest, not by fusion.** The worry that motivated embedding —
template `v1` silently running a different `merge.js` — is a *mutable tag* problem. Each
step template pins its image **by digest**, so
`df-step-merge-v1` resolves to one known digest at any moment, and every change to it is a
reviewed platform commit, never a tag moving under it. Tenants still cannot change it: the
digest lives in a platform-owned, cluster-scoped template.

**What the template adds to the image** is declared there, not in the image: credentials
mounted by name, and posting the verdict (§3.1).

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
`max-concurrent-runs`, `run-timeout-seconds`. Claim TTLs are not here, or anywhere: sandbox lifetime is `shutdownTime`, set per claim and per step (§3.2). Per-step tuning travels in
`DF_CONFIG`. The alternative — the Sensor injecting every setting as an argument — makes
every trigger carry every setting.

Settings are read when a run starts, so a change applies to the next run. The ConfigMap
needs the label `workflows.argoproj.io/configmap-type: Parameter` or Argo ignores it, and
the controller's `argo-cluster-role` must grant `configmaps get/list/watch` — checked on
`dora-showcase` with `kubectl get clusterrole argo-cluster-role -o yaml`, and re-checked on
each hub by the implementation (§7). The design rests on one unobserved behaviour here — that these
defaults resolve in the Workflow's namespace under `clusterScope: true`; §7 makes it a
go/no-go gate and names the fallback. A step a team does not want is simply absent from its process,
so there are no `*-enabled` keys and no skipped nodes in the UI.

### D12. Fixed contract names; one factory per namespace

Fixed names keep templates readable; deriving them from a `factory` parameter would make
every name an expression, and some fields (template-level executor SA, ConfigMap refs used
to *resolve* parameters) cannot be safely templated. The namespace is Kubernetes' isolation
boundary for RBAC, Secrets, NetworkPolicy and quotas — two factories sharing one would
share credentials, blast radius and sandboxes in one isolation domain.

The workflow ServiceAccount is `dark-factory`, not `context.name` as the OAM authoring rules
prescribe ([`.kiro/steering/oam-authoring.md` §3](../../.kiro/steering/oam-authoring.md): "A
component that runs a workload owns its ServiceAccount, named `context.name`"): it must never gain an AWS identity (D16), so not being a trait anchor is a
feature.

### D13. Control-plane isolation keyed on a pod label

On EKS VPC-CNI an ordinary NetworkPolicy does not reliably apply to Sandbox-owned pods, so
the real isolation is an Admin-tier `ClusterNetworkPolicy` selecting
`agent-sandbox.io/role in (coder, step, clone)` in any namespace — covering every factory's
coder, every holdout launch and every workspace clone pod (D4) automatically. `type: sandbox`
stamps `role: coder` and `role: step` from the platform-owned Application (§3.2), and the
component stamps `role: clone`, so coverage does not depend on any image cooperating — and
it is why #151 needs a platform-settable role (§3.5): a sandbox rendered as `session` would fall
outside it. A per-factory policy would mean a tenant
Application owning cluster-wide admin-tier network policy.

Namespaced NetworkPolicy for the sandboxes is #151's to manage, as `type: sandbox` renders
it; the factory adds nothing namespaced that could contend with it.

**Ingress** is denied to `role in (coder, step)` from everywhere except the pod's own
namespace, by a third rule in the same Admin policy, matching #151's limit of sandbox ingress
to the claimer's namespace.

**Egress is by address range, not by host.** The label-keyed Admin policy for
`role: step` always denies the cluster's service and pod ranges, the VPC's private ranges
and the instance metadata endpoint, and allows DNS only to the cluster resolver the
sandbox's DNS policy points at — the kube-dns ClusterIP `/32` on port 53 — since the service
range is otherwise denied (§7 confirms the resolver for #151's templates). Where
`factoryPolicy.stepEgress` is `public`, the runner stamps holdout's install launch
`agent-sandbox.io/egress: public`, and a second rule allows that label TCP 443 to any
remaining address; the clone pod (D4) gets the same rule. Everything else is denied. These are CIDR rules, which
`ClusterNetworkPolicy` enforces with VPC CNI 1.21.1 or later. EKS documents network policy
for **EC2 Linux nodes** only, so this is not "every node type": it holds on the Kata pools
(self-managed Karpenter on EC2) and, per the EKS security-model page for Nitro Sandboxes, on
Nitro under Auto Mode, but each runtime is
confirmed by §7's per-runtime test before `stepEgress: public` may be enabled on a cluster.
The service range must be in the deny list explicitly, because VPC CNI matches egress before
Service address translation.

**Holdout's test launch gets no network at all — DNS included.** It runs coder-written code
with the scenarios mounted, and DNS alone is enough to exfiltrate them as queries. The runner
labels that launch `dark-factory.io/launch: holdout-test`, and a fourth Admin rule, at a
higher priority than the DNS allow, **denies all egress** for that label, port 53 included. On
Nitro the launch is also `networkIsolated`, so there is no network device to filter.

**Start-up window.** In VPC CNI's default `standard` enforcing mode a new pod is
default-allow until its policies are programmed (observed in `30-networkpolicy.yaml:48`).
Requiring `NETWORK_POLICY_ENFORCING_MODE=strict` would close it, but strict mode is
cluster-wide and needs a policy for every pod. Instead, on Kata the holdout test launch starts
with an init container that probes a public IP address (no DNS needed) and does not exit
until the probe is refused, so the test container — the only one that mounts the scenarios —
never runs inside the window. On Nitro, `networkIsolated` leaves no window to wait out.

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
| No egress at all | Most repos' dependency installs need a package registry |

**Cost:** an install launch with public egress can reach any public host, so coder-written
install scripts can send out the source they are given (§5). Environments that need a host allowlist apply one at the VPC (AWS Network
Firewall or Route 53 Resolver DNS Firewall), outside the factory.

**The two Admin policies must not share a priority.** (The label-keyed policy's rules — step
egress, DNS, ingress and the holdout deny — are ordered within it.) Both default to `adminPriority: 10`,
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
  namespace grants only `get` and `update` on the four `dark-factory-github-*` Secrets
  (`resourceNames`). Kubernetes RBAC cannot restrict `create` by name, and the provider would
  otherwise create the Secret on its first push, so **onboarding pre-creates** the four
  Secrets, empty, and the Role never needs `create`.
- Per factory, one **`PushSecret` per role** in the platform namespace, with
  `selector.generatorRef` pointing at that role's generator and `secretStoreRefs` pointing at
  that store, and `refreshInterval: 20m`. Each refresh mints a new token and writes it as
  `dark-factory-github-<role>` (§3.2). The default would be one hour, a token's whole
  lifetime; 20 minutes leaves every token at least 40 minutes of validity after the kubelet
  syncs it, which covers a 30-minute run.

So the chain is generator → `PushSecret` → Kubernetes-provider `SecretStore` → native Secret in
the factory namespace. Nothing in the factory namespace pulls: pods mount the Secret like any
other, and the namespace holds no generator, no key and no External Secrets objects for these
tokens — only the four Secrets. The generator runs only beside the key, so the key never
needs to be anywhere else; only the minted output crosses into the factory, written by the
External Secrets controller, so no factory pod holds a Kubernetes token (D4, D16).

**What bounds a namespace to its repos.** Every object above is created by the platform's
**onboarding** (D20), not by the Application, and all of them live in the platform namespace,
where no team has access. Together they are the factory's **onboarding record**: the repos
and the installation ID a token may name. A factory covers **one repo owner**, because one
App installation covers one owner and each role has one generator: the component rejects a
second owner in `repositories` at render. Editing `repositories` in an Application cannot widen
what a token reaches; a repo in the Application but not in the record gets no token, and
status reports it (D20).

| Token | Used by | Scope |
|---|---|---|
| coder | The coder VM (0400, never in env) | Issues read; contents and PRs write. **No checks** — `platform/code` posts for it |
| orchestrator | **Platform steps only** | Adds `checks: write` and Issues write. The only token that can post a verdict merge accepts |
| events | EventSource only | Repository webhooks write, for webhook registration. The HMAC secret still comes from Secrets Manager |
| reader | D4 clone pods only — platform pods, never a sandbox; supplied through a git credential helper | Contents read. Nothing else |

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

Holdout's launches get none of these (D4). The most exposed components hold the least
privilege, and the cluster prefix lets each cluster's External Secrets identity be limited
to its own paths.

### D16. No AWS identity anywhere in a factory

No factory ServiceAccount carries IAM, and `factoryPolicy` forbids attaching an identity
trait to one. Holdout's launches see no GitHub credential at all: the D4 reader token is
mounted only in the platform clone pod that fills their workspace volume.
`platform/security-scan` waits for the Security Agent **GitHub App** and
mirrors its verdict rather than calling AWS APIs; the chart's IRSA-plus-S3 mode is not
ported. The coder and holdout's launches execute untrusted code in a shared namespace, so
having no AWS credentials there at all is simpler and stronger than scoping them per step.
A per-step scoped identity for the Security Agent's headless API path was considered and
rejected for v1: it still lives on a ServiceAccount in a namespace that runs untrusted code,
and that path needs account-specific agent-space and role values the public chart must not
default. Revisit if the App path proves unworkable.

**Cost:** `security-scan` needs the App installed per repo, and a future `deploy` step is
harder than it would be with IRSA. Intended — cloud access, when a step needs it, gets a
scoped credential, not an identity in the namespace.

### D17. Holdout scenarios: a synced Secret

Scenarios live in `dark-factory-holdout`, one key per repo (`<owner>-<repo>.json`), synced
from the secrets store like the GitHub tokens; `platform/holdout` mounts it read-only and
optional. They are the one piece of genuinely confidential tenant data in the factory, so
the secrets store is where they belong — a hand-applied ConfigMap would sit outside GitOps
and outside status. They stay out of the Application, because an Application living in the
target repo would let the coder read them, and only holdout's test launch ever mounts them,
which is why holdout cannot be team-ownable (D1). A repo
with no key fails closed (`NOT VERIFIED`) while `requireScenarios` is true, the default.

**Execution.** `platform/holdout` splits in two. The **tests**, which run coder-written
code, go through the D4 runner: an agent sandbox with no token, the workspace,
and **only this repo's scenario key** mounted (a Secret volume `items:` entry set from the
run's `<owner>-<repo>`). The **judge and verdict** stay in the platform step, which reads
the test launch's results from the runner, has the judge score them, and posts **only the
judge's counts** — never the test launch's log or `summary`, which coder code wrote with the
scenarios in reach (§3.1). Coder output never shares a pod with a credential or with another
repo's scenarios.

The test launch **gets no network at all, DNS included**, whatever `stepEgress` says: the
scenarios are mounted there, and even DNS queries could carry them out. A dedicated Admin
rule denies all its egress (D13). It also never starts inside the pod-start default-allow
window: on Nitro it runs `networkIsolated`, with no network device at all; on Kata its first
init container, which does not mount the scenarios, waits until egress is refused (D13), and
only then does the test container that mounts them start. It mounts the workspace
**read-only**, so nothing it does persists for a later launch (D4).

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

In v1 it is also how a team brings its **own tooling** into the process: anything the team
already runs as a GitHub check — contract tests, a licence scanner, a compliance tool — gates
the factory through `ci-check`, and a failure feeds `onFail: revise` like any other step.
Running bespoke tooling *inside* the factory is the later team-step version (§8). A timeout is
a failure, hence a refusal at merge.

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
annotation. The ClusterIP exists because the sandbox operator can give sandboxes
`dnsPolicy: None`, on every runtime; since the factory's sandboxes are #151's (§3.2), they
reach Bifrost exactly as any `type: sandbox` does, and nothing is per runtime. The ClusterIP is
admitted by a `/32` egress rule, because VPC-CNI matches egress on the pre-DNAT ClusterIP,
and must stay in sync in three places: the bifrost chart's `staticService.clusterIP`,
agent-sandbox's `networkPolicy.bifrostClusterIp`, and this value. `webhook.host` defaults to
`ingressDomain`, and factories are told apart by D14's `/dark-factory/<ns>/<name>` path,
as the hub factory does today (`registry/sandbox.yaml`). A per-factory host would sit two
labels below the domain, which a single-level wildcard certificate does not cover. Override
it only with a host the cluster's certificate already matches.

### D20. Status, and prerequisites the platform owns

An Application that claims to describe the factory, beside a factory that silently fails
because a secret is missing, is an abstraction leak.

`customStatus` and `healthPolicy` report the **expanded** process (stages, steps, each
resolved platform versions and digests), each prerequisite's state (the App installation
and the four minted tokens, the holdout source per repo, the `dark-factory-sandboxes`
Application's health, webhook registration), and which `factoryPolicy` applied. The platform also takes over
namespace creation, `webhook.host` defaulting (D19), and the holdout source as a
secrets-store reference (D17); the team keeps what lives in GitHub — the Apps, branch
protection.

**Sandbox health is #151's:** the `dark-factory-sandboxes` Application reports
`type: sandbox`'s own health policy (`readyReplicas >= spec.replicas` on the pool), and the
factory's status reads that Application rather than re-deriving it.

**Onboarding and tenancy.** The namespace is the tenant boundary, so the team must not
administer it. Onboarding is an entry in the `dark-factory-templates` chart's
`onboarding.factories` values, reviewed and synced like any platform change — one entry per
factory: namespace, repo owner and names, installation ID, coder image, coder pool size and
throttles. It writes the namespace's **onboarding record** — the platform-namespace External
Secrets objects that mint and push its tokens, plus the scoped Role (D15) — and creates
`dark-factory-<team>` with:

- the platform-owned `dark-factory-sandboxes` Application (§3.2), applied by the platform's
  GitOps identity;

- a team Role that may apply and read the `Application`, and read Workflows, pods and logs —
  **no** Secrets, Pods, Workflows or RBAC writes;
- **one admission policy on Applications.** Applying an Application is not narrow on its
  own: vela-core applies with its own identity unless `authentication.enabled` is set
  (default `false`), and `k8s-objects`, `ref-objects`, the `json-patch` and
  `json-merge-patch` traits, policies and workflow steps can each make it create or rewrite
  objects the team Role denies. The rule is an **allowlist**, so fields vela adds later
  (`spec.workflow.ref`, vela 1.12's `spec.sources`) are rejected by default: in a factory
  namespace an Application's `spec` may contain **only `components`**, and each component
  **only `name`, `type: dark-factory`** (no `@version`) **and `properties`**. It is a
  `ValidatingAdmissionPolicy` with `failurePolicy: Fail` on CREATE and UPDATE, bound to
  namespaces carrying the onboarding label. A second binding denies any `*Definition` kind
  in factory namespaces, because KubeVela resolves `type` in the Application's own namespace
  before `vela-system`, so a team-local `dark-factory` definition would otherwise win.

  vela then can only create what the platform-written `dark-factory` definition renders, and
  that definition exposes **no pass-through pod fields** — no raw pod spec, volumes,
  env-from-secret or labels; properties are typed values, and code that is not the platform's runs
  only in the coder sandbox and holdout's launches (§3.1). No team property chooses an image: the only image that runs beside a
  token, the coder's, is set in the onboarding entry, digest-pinned from `allowedRegistries`
  (D7). The platform's own `dark-factory-sandboxes` Application is the one Application in the
  namespace the rule does not apply to; the policy exempts the platform's GitOps identity by
  name, and the team's Role cannot act as it.

  This is the only tenancy control, so it is written to stand alone: the team can create nothing but its Application, and
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
- a ResourceQuota sized for the coder pool, `maxConcurrentRuns` coder claims and holdout
  launches, and their workspace PVCs (count and `requests.storage`), **including
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
| `synchronization.mutexes` (list) | Argo v4 removed `mutex`: `argo lint` on v4.1.4 rejects it, and the list form works on v3.6+. |
| No issue title in the claim | Raw issue text broke the claim manifest on quotes. The coder reads the title from the issue it fetches. |
| Revision notes base64-encoded | Arbitrary markdown cannot break the claim manifest. |
| Deterministic Workflow names per event | GitHub redelivers webhooks; duplicates collide (`AlreadyExists`) instead of racing. |
| Registry gate `enable_dark_factory_templates`, requiring `enable_agent_sandbox` | Not `enable_dark_factory`: since #151 that label also starts the agent-sandbox chart's own coder warm pool, which would run beside each factory's `dark-factory-coder` pool. The operator and CRDs come from `enable_agent_sandbox` (set by `sandbox.enabled`). |

---

## 5. Security model

| Threat | Control |
|---|---|
| Coder code reaches credentials | Agent sandbox (own kernel); no Kubernetes token, no IAM; only a narrow, hour-long GitHub token at `/etc/secrets` (0400) |
| Coder or holdout launch reaches the control plane | Admin-tier ClusterNetworkPolicy on `role in (coder, step, clone)` (D13); DNS only to the cluster resolver, public 443 only where policy grants it; private ranges always denied; ingress only from the pod's own namespace (D13) |
| Another token posts a same-named passing status | Merge and the ruleset accept only check-runs from the platform App; only the orchestrator token can create them (§3.1, D15) |
| A team runs its own code as a step | Not possible in v1: every `use:` must name a `platform/` step, checked at render (D3, D7) |
| Coder games its own tests | Hidden scenarios no step but `platform/holdout` can reach (D17); a judge from a different model family; fail-closed on missing scenarios |
| Coder output executed on the hub (holdout) | Runs through the D4 runner: no token, one repo's scenarios, agent sandbox (D17); `npm install --ignore-scripts`; no AWS identity in the namespace (D16) |
| Holdout's test launch runs before its egress policy applies | `networkIsolated` on Nitro; on Kata an init container holds the test container until egress is refused (D13, D17) |
| Holdout's scenarios leave through DNS, logs or the workspace | The test launch's egress is denied outright, port 53 included (D13); its log and `summary` are never surfaced, only the judge's counts (§3.1, D17); it mounts the workspace read-only, and each run's PVC is unique and deleted after use (D4) |
| Team creates arbitrary objects through its Application | An allowlist `ValidatingAdmissionPolicy` (`failurePolicy: Fail`): `spec` holds only `components`, each only `name`, `type: dark-factory` and `properties`; no `*Definition` kinds in the namespace (D20) |
| Team pod mounts a factory GitHub token | The team can create only its Application, which admission limits to `type: dark-factory` and its properties; that definition exposes no pod fields and no image choice; the coder image is set by onboarding, digest-pinned from `allowedRegistries` (D7, D20) |
| A team gives its own sandbox a factory role | #151's role property admits `coder`/`step` only from the platform's GitOps identity (§3.5) |
| A factory obtains tokens for repos it was not onboarded for | Tokens are minted in the platform namespace from the onboarding record, which no team can write; the push is bounded by a `get`/`update`-only, `resourceNames`-scoped Role to that factory's own pre-created token Secrets (D15) |
| A factory syncs another factory's secrets | The secrets-store prefix is derived from namespace and name, with no override (§3.4) |
| Unreviewed or failing code merged | Merge only on a human approval event; `platform/merge` refuses unless every required status is green at the PR head; a missing status is a refusal |
| Tampered platform step code | Each platform step's image is digest-pinned in a cluster-scoped, platform-owned template (D10); only the platform changes the digest, through a reviewed commit; each run records the digest it ran (D9) |
| Tenant process drops a required gate | Three overlapping enforcement points (D7) |
| Runaway revision loop | One iteration budget shared by `onFail: revise` and comments (D8); factory-authored comments filtered at the Sensor |
| Two runs for one issue holding two VMs | Mutex and claim keyed on owner, repo and issue number (D21); `maxConcurrentRuns` semaphore (D8) |
| A holdout launch reaches another factory | The `role: step` Admin policy always denies the cluster's service and pod ranges and the VPC's private ranges; granted egress is public addresses only (D13) |
| Cross-factory interference | One factory per namespace (D12); per-namespace events and secrets (D14, D15); Argo locks are namespace-scoped |

**Residual risks**

- **The ruleset is the only hard stop on self-merge** (D20). Rulesets on private repos are
  unavailable on GitHub's free plan. With a repo-level ruleset, the team's repo admins can
  switch it off; the factory then refuses to merge, but a human could still merge in the UI.
  An organization ruleset removes that gap.
- **Render-time policy binds only the component path.** Team RBAC removes raw Workflow
  submission for the team (D20); a cluster admin can still bypass it.
  `templateReferencing: Strict` would close it cluster-wide (§8).
- **A valid process can still be a bad process**: a team can compose something that passes
  policy and verifies very little, or gate on a `ci-check` whose check always passes. Policy
  floors *our* gates, not the quality of a team's.
- **Holdout's install launch can exfiltrate the source it is given** where `stepEgress:
  public` allows it: coder-written install scripts see the branch and can post it to any
  public host — v1 has no per-host limit (D13). That is why `stepEgress` is per-environment
  policy rather than a team setting, defaults to `none`, and why an environment needing a host
  allowlist applies one at the VPC.
- **The App private key is the highest-value secret in the system.** It can mint any
  permission the App has on every installed repo, across every factory. It lives only in a
  platform namespace, read only by External Secrets' generator (D15); a compromise there is a
  compromise of every factory.

---

## 6. Operations

**Platform, once per hub.** Register the platform GitHub App, store its private key in
the platform namespace beside the External Secrets generator that mints from it (D15). The sandbox runtime comes
from the cluster, as for `type: sandbox`: Nitro needs nothing; a Kata cluster sets
`sandbox_runtime_class: kata-clh` and `confidential_sandbox_runtime_class: none` through
`<hub>/config` (#151 `registry/agentcore.yaml:82-84`) — `sandbox_runtime: kata` alone moves
only the agent-sandbox chart's pool. Make the platform step images pullable in this hub's
account, so the digests their templates pin resolve (D10). Set `enable_agent_sandbox`
(through `sandbox.enabled`) for the operator and CRDs, and `enable_dark_factory_templates`
for `dark-factory-templates` — not `enable_dark_factory`, which since #151 also starts the
chart's own coder pool (D21). Set `factoryPolicy` for the
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

**Team, for its own tooling.** Run it where the team already runs CI, as a GitHub check, and
add it to `process` with `platform/ci-check` (D18).

**Changing the process.** Edit `process`; KubeVela re-renders and the next run uses it.
In-flight runs finish on the old shape. A violation of `factoryPolicy` fails at render.

**Upgrading platform steps.** A compatible fix keeps its version; an incompatible change
ships at a new one, and factories move by bumping `catalogVersion` or that step's `version`,
with the old template kept until every factory has moved (D9). Updating the coder image is
a change to the factory's onboarding entry.

**Deleting a factory.** Deleting the Application removes everything the component created in
the namespace:

- **In-flight runs** are deleted with the namespace's Workflows; their SandboxClaims, holdout
  `Sandbox`es and workspace PVCs go with them, and `shutdownTime` (§3.2) reaps any sandbox the
  deletion misses.
- **Webhooks** registered by the EventSource are deregistered on its deletion; status
  reports any it could not remove.
- **Minted tokens expire within the hour**, and stop being refreshed once offboarding
  removes the namespace's onboarding record. The team uninstalls the platform App from
  repos no other factory watches.
- **Open `df/` PRs and branches stay open**, so work in progress is not lost silently; their
  `dark-factory/*` checks can no longer turn green.

The namespace, the `dark-factory-sandboxes` Application and the token objects are removed
through onboarding (D20), not the team's Application.

---

## 7. Validation

Already established, on the substrate rather than this design's shape: `mutexes` accepted
and `mutex` rejected on Argo v4.1.4; a server-side dry run on `dora-showcase` for the
component's namespaced objects (Sandbox and ExternalSecret CRDs are absent there); and the
holdout evaluator against sample scenarios with a stub judge that always says yes — an
honest implementation passed 4/4, a hard-coded one failed 2/4, to be re-run in the
`platform/holdout` image (D10).

**Gates for the implementation, not this design.** The live end-to-end run and the
`configMapKeyRef` go/no-go below are run by the implementer before the implementation merges,
and their results are recorded in that PR. If the go/no-go fails, the implementation uses
D11's fallback (settings rendered as literal defaults into `dark-factory-process`). This
design doc does not wait on either.

Needed, none of it done:

- **Token minting (D15):** a namespace receives tokens only for the repos in its onboarding
  record; a repo added to the Application but not the record gets none and appears in
  status; a factory's push SecretStore can only `get`/`update` that factory's four
  pre-created token Secrets and cannot read the App key; tokens are refreshed every 20 minutes
  and stop after offboarding; a second repo owner fails render.
- **Go/no-go: workspace PVCs on each runtime (D4).** Nitro mounting EBS is documented; still to
  confirm are a Nitro holdout launch mounting the workspace PVC **under `networkIsolated`**, and
  a Kata one mounting it, each after a clone pod has released it, in the clone pod's zone. If a
  runtime cannot, holdout on it falls back to the runner streaming the workspace over `exec`
  (D4's options table).
- **#151's role property (§3.5):** `type: sandbox` renders `role: coder`/`step` for the
  platform's GitOps identity, and rejects either from any other requester, in any namespace.
- **Holdout launches from templates (D4):** a `Sandbox` the runner instantiates from
  `dark-factory-holdout-install` or `-test` differs from the template only in the run-specific
  fields §3.2 lists — checked by diffing the two.
- **Tenancy (D20):** in a factory namespace, the admission policy rejects an Application
  with any `spec` field but `components` (including `workflow`, `workflow.ref`, `policies`
  and `sources`), any component field but `name`, `type` and `properties`, a `type` other
  than `dark-factory` or with an `@version`, and any trait; any `*Definition` is rejected; no
  `dark-factory` property can reach a rendered pod's spec, volumes, env sources, labels or
  image; a `use:` outside `platform/` fails render; the platform's `dark-factory-sandboxes`
  Application is admitted only from the platform's GitOps identity; a tag-pinned or
  off-registry coder image fails onboarding; and the secrets-store prefix cannot be
  overridden.
- **Holdout leaks (D17):** the test launch's DNS queries are refused; its log and `summary`
  appear nowhere on GitHub or in revision notes; its workspace mount is read-only; a second
  run of the same issue gets a new PVC.
- **Go/no-go: `configMapKeyRef` defaults resolve in the Workflow's namespace** for a
  Workflow submitted against the namespaced `dark-factory-process` whose tasks are
  `templateRef` with `clusterScope: true`. Gates any factory beyond the
  hub. If it fails, the fallback is for the component to render settings into
  `dark-factory-process` as literal parameter defaults, which it already renders per factory
  (D5).
- **A conformance suite for the step contract** — a reference step in two languages, plus
  adversarial ones: no result file, malformed JSON, non-zero exit, a result claiming
  `success` after an internal error, and holdout launch images reaching for the API server,
  a GitHub token and the network. Those last must fail closed. Also: a passing
  `dark-factory/*` status or check-run not created by the platform App must be refused by
  `platform/merge` (§3.1). The same suite becomes the team-step conformance test later (§8).
- CUE type-checks: each preset, `addSteps`, an explicit process, a violation of each
  `factoryPolicy` rule (must fail naming the rule), and a `use:` or `requiredSteps` entry
  naming a non-`platform/` step (must be rejected).
- A rendering golden test per preset: stage order, step set, and the required-check list
  `platform/merge` receives.
- Per-platform-step tests against the contract, each starting with empty `DF_PR_NUMBER`.
- `helm lint` and `argo lint` on every platform step template and the runner; every
  `configMapKeyRef` key present in the rendered `dark-factory-config`.
- Both Admin ClusterNetworkPolicies admitted with distinct priorities on a live VPC-CNI, and
  **per runtime** (Kata on Karpenter, Nitro on Auto Mode) that `role=step` covers holdout's
  launches and that D13's CIDR rules are enforced for the guest — the gate
  for enabling `stepEgress: public` on that cluster. On Nitro, also whether `domainNames`
  rules are enforced for a guest (§8).
- Holdout's test launch: on Kata, the init gate holds the test container until a public
  probe is refused; on Nitro, `networkIsolated` leaves no network device (D17).
- A `$DF_RESULT` written inside a Kata guest, and inside a Nitro `networkIsolated` guest,
  arrives as the pod's termination message; a finished holdout `Sandbox` reaches `Succeeded`
  (`restartPolicy: Never`).
- Which resolver #151's templates configure, that D13 allows exactly it on port 53, and that
  nothing else in the service range is reachable (D13).
- `scripts/nitro/verify-isolation.sh` against a claimed coder and a holdout launch,
  showing each runs in its own guest kernel.
- A step result over 4 KB, or malformed, is reported as `failure`; a step's full log is linked from its check-run (§3.1).
- Sandbox cold-boot latency per runtime for holdout's launches, to decide whether they need
  a warm pool.
- A live run on `preset: standard` (label → VM → PR → steps → approval → merge), then one
  with a `ci-check` failure and `onFail: revise`.

---

## 8. Follow-ups

1. **Team-authored steps — the next version.** Lets a team run its own step image inside
   the factory, addressed `use: <team>/<name>`. It adds, and v1 deliberately does not have:
   the §3.1 contract published as a versioned ABI; a step registry and later a `shared/`
   tier; the runner launching team images in `type: sandbox`-rendered sandboxes; per-step
   capabilities (`workspace`, `egress`, `secrets` from a team-owned prefix); and
   `allowedRegistries` with digest pinning for team images. Its safety rests on three
   invariants that must hold before it ships: a team step holds **no credentials** and cannot
   post a status; it has **no merge authority**, because `requiredSteps` stays limited to
   `platform/` steps; and it runs **isolated**, as holdout's launches do (D4, D13). It starts
   once the platform steps run end to end.
2. **Cosign verification of images**, alongside `allowedRegistries`.
3. **A warm pool for holdout's launches**, if sandbox boot latency proves to matter.
4. **Move the chart's hub factory onto the step templates**, removing the last duplicated
   pipeline. **Port the chart's templates to `mutexes`** — they fail on Argo v4 today.
5. **Evaluate `templateReferencing: Strict`** on the hub's Argo controller — the only thing
   that would close the inline-Workflow bypass.
6. **Update `setup-secrets.sh`** for the App key, per-factory paths and the holdout source.
7. **Implement D10:** split the chart's scripts into one image per platform step, each
   honoring the contract; pin each by digest in its template; stand up the build-and-publish
   pipeline.
8. **A reconciler for status** instead of long `customStatus` expressions (D20).
9. **Per-host egress** for holdout's install launch, through `domainNames` on clusters on the Auto Mode Nitro path
   once §7 confirms it is enforced for a guest, or a VPC firewall elsewhere (D13).
10. **Shrink the reserved set** per D1's principle, once team steps exist: `changeset` decided
    from a team-supplied path list, and holdout's evaluator as a team step given scenarios it
    cannot exfiltrate.
13. **A second forge**, through the four seams in §2.
11. **Per-repo concurrency limits** alongside the per-factory `maxConcurrentRuns` (D8).
12. **Deliver run inputs without claim env**, so the coder warm pool is actually adopted and
    its size can default above 0 (§3.4).

---

## Appendix: files

Planned layout. This branch holds only this design doc; of the paths below, only the chart
sources, `31-clusternetworkpolicy.yaml` and `PROFILES.md` exist on `main` today.

| Path | Role |
|---|---|
| `platform/oam/definitions/components/dark-factory.cue` | The component: per-namespace resources, `process` schema, step resolution, preset expansion, policy validation, rendered process |
| `gitops/addons/charts/oam-agent-components/templates/dark-factory.yaml` | Generated by `platform/oam/generate.sh`. **Checked in**; the chart cannot register the type without it |
| `docs/dark-factory/STEP-CONTRACT.md` | The step contract (§3.1), internal in v1 and published when team steps arrive (§8) |
| `gitops/addons/charts/dark-factory-templates/` → `onboarding.factories` | One entry per factory (namespace, repo owner and names, installation ID, coder image, pool size, throttles). Renders the namespace, team Role, Application admission policy, ResourceQuota, the `dark-factory-sandboxes` Application and the D15 External Secrets objects (§3.2, D15, D20) |
| `gitops/addons/charts/dark-factory-templates/` | Platform step templates (each pinning its image digest), the step runner, the catalog ConfigMap, the contract, `factoryPolicy` |
| `examples/dark-factory/platform-steps/` | One image per platform step, built from the chart's scripts (D10) |
| `platform/oam/examples/example-dark-factory.yaml` | Example: preset form, plus a `ci-check` on a team's own check |
| `gitops/addons/registry/sandbox.yaml` → `dark-factory-templates` | Registers the chart under its own `enable_dark_factory_templates` gate (D21) |
| `gitops/addons/charts/agent-sandbox/templates/31-clusternetworkpolicy.yaml` | Admin-tier policy keyed on `role in (coder, step, clone)` — egress, DNS, ingress and the holdout-test deny — distinct priority (D13) |
| `gitops/addons/charts/dark-factory/scripts/`, `holdout/evaluate.js` | Source the platform step images are built from, until the hub factory moves onto them (§8 item 4) |
| `platform/oam/definitions/components/sandbox.cue` (#151) | Gains the platform-settable `role` parameter and its admission rule (§3.5) |
| `docs/dark-factory/PROFILES.md` | Why there are no language profiles, and why process presets differ (D6) |
