# Design: Dark Factories as OAM components

| | |
|---|---|
| Status | Proposed; implementation in progress |
| Components | `dark-factory` OAM component, `dark-factory-templates` platform chart |
| Depends on | #151's `type: sandbox`, unchanged (§3.5) |

---

## 1. Summary

A **Dark Factory** turns a labelled GitHub issue into a reviewed, gated pull request. A team
gets one by applying a KubeVela `Application` that describes **its own delivery process**,
built from the platform's catalog of steps:

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

Teams do not share one SDLC: they differ on which checks gate a change, in what order, where
humans approve and what happens when a check fails. So the **team owns the process**, and the
**platform owns the steps** and the minimums no process may go below.

| Layer | Owner |
|---|---|
| **The merge decision** — whether a PR may merge, and which checks are required | Platform |
| **Isolation** — where code runs, what it can reach, which credentials exist | Platform |
| **Guardrails** — minimums per environment (`factoryPolicy`, D7) | Platform |
| **Steps** — what each step does (§3.3) | Platform in v1, **customized by each team through typed parameters** (D22). A team brings its own tooling in through `platform/ci-check`, which gates on any GitHub check the team already runs (D18) |
| **Process** — stages, order, approvals, failure handling (D5–D8) | The team |

Why the process can belong to the team: a fixed pipeline shape is not a security control. A
namespace admin who can create a Workflow can bypass any shape, so what actually holds is the
merge decision and the GitHub ruleset (D7, D20).

**Team-written step images come in a later version (§8), not v1.** They would need a published
step ABI, a registry, a runner for arbitrary images, and per-step capabilities and egress —
a large surface to maintain before a single platform step has run end to end. The `use:` field
already addresses steps as `<tier>/<name>`, so `use: acme/...` can be added later without
changing the developer API. v1 rejects any `use:` outside `platform/` at render. D22 lists
what v1 builds so team steps — including small scripts between platform steps — stay possible.

### Terms

| Term | Meaning |
|---|---|
| **Hub** | A cluster that runs Dark Factories |
| **Factory** | One team's `dark-factory` Application and everything it creates, in its own namespace `dark-factory-<team>` |
| **Onboarding** | The platform-owned step that creates a factory's namespace, credentials and sandboxes (D20). The team cannot change what it creates |
| **Run** | One Argo Workflow processing one issue (or one revision of it) |
| **Coder** | The agent that writes the code, inside a sandbox |
| **Sandbox** | A VM-isolated pod (Nitro or Kata) rendered by #151's `type: sandbox` |
| **Holdout** | Hidden test scenarios the coder never sees, run against its branch (D17) |
| **Launch** | One sandbox the runner starts for holdout: the **install** launch, then the **test** launch (D4) |
| **Runner** | The platform template that starts holdout's launches and reads their results (D4) |
| **Verdict** | A step's pass/fail result, posted as a GitHub check-run by the platform App (§3.1) |

### How a run works

1. A labelled issue reaches the factory's webhook. The Sensor starts a run of the team's
   rendered process (D5, D14).
2. **`platform/code`** claims a coder sandbox. The coder writes code, pushes `df/issue-<n>` and
   opens a PR (§3.2, §3.3).
3. The **verify** stage runs its steps in parallel. `platform/holdout` clones the branch onto a
   per-run volume, installs dependencies in one sandbox and runs the hidden scenarios in a
   second, network-less sandbox; a judge scores the results (D4, D17). `ci-check` and
   `security-scan` wait for external checks (D16, D18).
4. Each step's verdict is posted as a check-run **from the platform GitHub App** (§3.1). A
   failure with `onFail: revise` sends the coder back with the failure summary (D8).
5. **`platform/approval`** ends the run until a human approves the PR. The approval starts a
   continuation run (D8).
6. **`platform/merge`** re-checks every required verdict at the PR head and merges. The GitHub
   ruleset enforces the same checks, so nothing else can merge around it (D7, D20).

---

## 2. Scope

The coder runs model-written code prompted by public issue text, and holdout runs that code's
tests. Every decision below keeps both away from credentials, the merge decision and the
cluster control plane.

**Building in v1**

- Platform steps `code`, `changeset`, `ci-check`, `holdout`, `security-scan`, `approval` and
  `merge`, as images that follow one internal step contract (§3.1).
- A `process` schema with presets and explicit stages (`onFail`, `skipWhen`), and
  `factoryPolicy` guardrails.
- The per-namespace factory: identities, four short-lived GitHub tokens, sandboxes, holdout
  scenarios, settings, webhook trigger and status.
- Sandboxes rendered only by #151's `type: sandbox` (§3.2). A factory runs on whichever runtime
  the **cluster** is configured for — Nitro by default, Kata by annotation — and never chooses
  one itself. Lambda MicroVM is a later runtime, not ruled out.

**v1 is GitHub only.** A second forge (GitLab, for example) would plug in at four seams.
Steps talk to the forge only through the platform's verdict-posting and token paths, so a new
forge means new implementations of these four seams, not changes to every step:

| Seam | GitHub in v1 | Where |
|---|---|---|
| Intake | Issue-labelled webhook into the EventSource | D14 |
| Verdict posting | Check-runs from the platform GitHub App, read back by `platform/merge` | §3.1, D7 |
| Token minting | `GithubAccessToken` generator → `PushSecret` | D15 |
| Merge barrier | A ruleset requiring the App's checks, verified at startup and before merge | D20 |

**Not building**

| | Why / instead |
|---|---|
| Team-written step images, a step registry, a `shared/` tier | A later version, once the platform steps run end to end (§8) |
| Signature verification of images | Digest pinning and an allowed-registry list ship now; cosign later |
| Forges other than GitHub | The four seams above are where one plugs in |
| `deploy`, environment promotion | Nothing needs it before merge gating works |
| Pluggable intake | `issue-labeled` with a configurable label; issue types and slash commands can be added |
| Autonomy 3.5 (low-risk auto-merge) | v1 targets the README's Level 3; needs `changeset` proven first |
| Per-host egress | v1 egress is public-or-none (D13); per-host is an option on Nitro later |
| Multiple factories per namespace | D12 |
| Chart parity: sticky comment, claim reaper, metrics, Lambda runtime | Follow-ups; none block a first factory |

---

## 3. Architecture

```
       ┌──────────────────── cluster scope (platform) ─────────────────────┐
       │ PLATFORM STEPS (ClusterWorkflowTemplates, one image each)         │
       │   platform/{code,changeset,ci-check,holdout,                      │
       │             security-scan,approval,merge}                         │
       │   └ merge is the ONLY arbiter of mergeability                     │
       │ STEP RUNNER   df-step-runner-v1  ← starts holdout's launches      │
       │ GUARDRAILS    factoryPolicy (chart values, per environment)        │
       │ ClusterNetworkPolicy   isolates coder + holdout launches          │
       └─────────────────────────────▲────────────────────────────────────┘
                                     │ templateRef (clusterScope: true)
      ┌──────────── namespace dark-factory-acme (team) ───────────────────┐
      │ PROCESS  WorkflowTemplate dark-factory-process  ← rendered         │
      │                                                                    │
GitHub ─webhook─► Ingress ─► EventSource ─► Sensor ─► Workflow             │
      │ Workflow pods ── run as SA dark-factory                            │
      │   read  ConfigMap dark-factory-config                              │
      │   read  Secret    dark-factory-github-orchestrator  (platform only)│
      │   claim coder sandbox  ─► sandbox (coder)                          │
      │   launch holdout install/test ─► sandboxes                         │
      │   mount Secret    dark-factory-holdout  (test launch only)         │
      │ SANDBOXES  type: sandbox, platform-owned Application (onboarding)  │
      └────────────────────────────────────────────────────────────────────┘
```

### 3.1 The step contract

Every platform step is an image (D10) whose entrypoint reads environment variables and writes
one result file. In v1 this is an **internal contract**: one shape for every step, so all
steps are tested the same way and the renderer needs no per-step knowledge. It is written so
it can become the published ABI when team steps arrive (§8).

| Env in | |
|---|---|
| `DF_REPO` | `owner/name` |
| `DF_ISSUE_NUMBER` | The originating issue. With `DF_REPO`, forms the run key (D21) |
| `DF_PR_NUMBER` | Empty before `platform/code` opens the PR |
| `DF_BRANCH`, `DF_BASE_BRANCH` | `df/issue-<n>`, and the PR base |
| `DF_STEP_NAME` | This step instance's name; it names the step's check |
| `DF_CONTRACT` | The contract version (`v1`), so a step can refuse a contract it does not know |
| `DF_CONFIG` | JSON of this step's `config` from the Application, validated against the step's schema (D22) |
| `DF_INPUTS` | JSON of the `outputs` of earlier stages' steps, keyed by step name (D22) |
| `DF_WORKSPACE` | The checked-out branch, for steps that use one |
| `DF_RESULT` | The path to write the verdict to |

**Out — two channels, kept apart:**

- **The verdict:** one small JSON file at `$DF_RESULT`, and nothing else:

  ```json
  {"status": "success|failure|skipped", "summary": "one line, at most 256 characters",
   "outputs": {"optional": "small string values for later steps"}}
  ```

  `outputs` is optional and must keep the whole file well under 4 KB. It is the **only** way
  data passes between steps; larger data stays in GitHub (the PR, the branch) or the run's
  workspace (D22).

- **The output:** whatever the step prints. Its logs are linked from the check-run, and on
  `onFail: revise` the coder gets the `summary` plus the log's tail (D8). **Exception:**
  holdout's test launch runs coder-written code with the scenarios mounted, so its log and
  `summary` are never linked, posted or sent to the coder. `platform/holdout` posts only the
  judge's counts (D17).

The verdict travels as the pod's termination message (D4). Its 4 KB limit is far above a
two-field verdict. A non-zero exit with no result file is a `failure`. A result that cannot be
parsed is also a `failure`, never a partial pass — including an oversized one, because the
kubelet keeps the *last* 4096 bytes and the `status` at the start is lost.

**Who posts verdicts.** A step's own logic never posts its verdict. Its template reads
`$DF_RESULT` and posts it as a **check-run from the platform GitHub App** (D15). That keeps
posting in one place (and the forge seam with it). A check counts as a verdict because of
**which App created it**, not its name:

- `platform/merge` accepts a required `dark-factory/*` check only if its `app.id` is the
  platform App's. A same-named status from anyone else counts as missing, which is a refusal.
- The branch ruleset requires each `dark-factory/*` check **from the platform App**, so GitHub
  enforces the same rule when a human merges in the UI.
- Only the orchestrator token has `checks: write`. The other three tokens are minted without
  it, so nothing else in a factory can create a platform check.

**Where non-platform code runs.** Exactly two places, both sandboxed: the coder, and holdout's
two launches (which run the coder's output). Neither can reach the orchestrator token, a
Kubernetes token, AWS credentials or the merge decision (D4, D16). The install launch gets
public HTTPS only where `factoryPolicy.stepEgress` is `public`; the test launch gets no network
at all (D13, D17).

### 3.2 The per-namespace contract

Steps find everything by a fixed name in the run's own namespace. The team's `dark-factory`
component creates the process and its plumbing; onboarding (D20) creates the credentials and
the sandboxes.

| Name | Kind | Created by | Purpose |
|---|---|---|---|
| `dark-factory` | ServiceAccount | Component | Workflow identity: claims the coder sandbox, launches holdout, submits revision runs |
| `dark-factory-executor` | ServiceAccount | Onboarding (`type: sandbox`) | Token-less identity of the holdout launches |
| `dark-factory-clone` | ServiceAccount | Component | Token-less identity of the workspace clone pod (D4) |
| `dark-factory-sandboxes` | Application (platform-owned) | Onboarding | The three `type: sandbox` components below |
| `dark-factory-github-<role>` | Secret ×4 | Onboarding (empty), filled by D15 | The GitHub tokens. The orchestrator token is for **platform steps only** |
| `dark-factory-config` | ConfigMap (label `workflows.argoproj.io/configmap-type=Parameter`) | Component | Factory-wide settings (D11) |
| `dark-factory-holdout` | Secret, optional | Component (ExternalSecret) | Hidden scenarios, one key per repo: `<owner>-<repo>.json` (D17) |
| `dark-factory-process` | WorkflowTemplate | Component | The rendered process (D5) |

**Sandboxes come from #151 and nothing else.** One definition owns how a sandbox is rendered:
#151's `type: sandbox`. The factory never renders a sandbox spec of its own, so a change to
#151 reaches every factory without an edit here. Onboarding creates a **platform-owned
Application**, `dark-factory-sandboxes`, in each factory namespace. The platform's GitOps
identity applies it, not the team. It has three components:

| Component | Image | Settings beyond #151's defaults |
|---|---|---|
| `dark-factory-coder` | The coder image from the onboarding entry, digest-pinned from `allowedRegistries` (D7) | `mode: isolated` (the coder needs `exec`, which `confidential` denies); `service: false`; `warmPool` from the onboarding entry, default `0` (§3.4); Nitro throttles if set |
| `dark-factory-holdout-install` | The platform holdout test image (D10) | `service: false`; `warmPool: 0` |
| `dark-factory-holdout-test` | The same image | `service: false`; `warmPool: 0`; `networkIsolated: true` on a Nitro class |

`service: false` is required on the test component: `type: sandbox` defaults `service: true`
and rejects it together with `networkIsolated`. Nothing reaches any of the three over HTTP.

Everything else is #151's, unchanged: the runtime class from the cluster secret, the render
guards, scheduling, pod annotations, `envVarsInjectionPolicy`, NetworkPolicy and DNS
management, pool health and the card. A Kata cluster sets `sandbox_runtime_class: kata-clh`
and `confidential_sandbox_runtime_class: none` through `<hub>/config` (#151
`registry/agentcore.yaml:82-84`). No team property reaches any of this; the coder image, pool
size and throttles are onboarding settings (§3.4).

**Why one Application per factory namespace, not one shared pool.** A claim must live in its
pool's namespace, and a pod can mount Secrets only from its own namespace. A shared pool would
mean one coder token for every team, every team's coder in one namespace, and every factory
able to see the others' claims. A per-namespace Application costs nothing at idle: the pool
defaults to zero, and the holdout components are only templates.

The pods keep #151's `role: session` label. The factory's network isolation finds them by
namespace and component name instead, so #151 needs no change (§3.5).

**How the factory uses the sandboxes**

- **Coder.** `platform/code` claims from `dark-factory-coder` and sets
  `lifecycle.shutdownTime` (the run timeout plus a margin, `shutdownPolicy: Delete`), because
  nothing else reaps a sandbox whose caller died. The claim must not repeat a variable the
  template already sets, such as `BIFROST_URL`, or operator v0.5.1 rejects it.
- **Holdout launches.** A claim cannot carry a per-run volume, so the runner creates a
  `Sandbox` from the template #151 rendered into `dark-factory-holdout-install` or `-test`. It
  adds **only** run-specific fields: the workspace volume (read-only for the test launch), this
  repo's scenario key (test launch only), `restartPolicy: Never`, `spec.shutdownTime` and a
  `dark-factory.io/launch` label. It never touches an isolation field, so #151's guards still
  hold (D4).

Claim and release are the one runtime-specific part (Lambda needs suspend and resume). They
belong in a per-runtime step that `platform/code` calls, so a new runtime does not change the
platform steps' versions (D9).

### 3.3 Platform steps

Each step follows §3.1 and posts its verdict as `dark-factory/<name>` (D7). Teams customize
a step through its `config`: each step publishes a typed, versioned schema of what a team may
set, validated at render (D22). The keys below are the starting set; adding a key is a
compatible change (D9).

| Step | Does | Config | Posts |
|---|---|---|---|
| `platform/code` | Claims a coder sandbox, runs the coder on the issue (or revision notes), pushes `df/issue-<n>`, opens or updates the PR, releases the sandbox | `engine` (default from `coder-engine`), `instructions` (a repo path of team conventions the coder must follow), `model` | `dark-factory/code` |
| `platform/changeset` | Compares the PR's changed paths with each stage's `skipWhen` and emits which stages to skip, as `outputs` (D8) | none (reads the rendered `skipWhen` lists) | `dark-factory/changeset` |
| `platform/ci-check` | Waits, with a time limit, for a named GitHub check and copies its result; a timeout is a failure (D18) | `check`, `timeoutSeconds`, `failureSummary` (how much of the check's output to send on `revise`) | `dark-factory/ci-check-<check>` |
| `platform/holdout` | Runs the hidden scenarios in two sandboxed launches (install, then test), then has a judge confirm them; fails if the repo has no scenarios (D17) | `threshold`, `requireScenarios`, `judgeModel`, `judgeRuns`, `judgeQuorum`, `testCommand`, `installCommand` | `dark-factory/holdout` |
| `platform/security-scan` | Waits for the Security Agent App's review and copies its verdict, failing at or above `blockLevel` (D16) | `blockLevel`, `waitSeconds`, `ignore` (finding IDs the team has accepted, each with a reason) | `dark-factory/security-scan` |
| `platform/approval` | Ends the run until a review arrives; passes when GitHub's `reviewDecision` is `APPROVED` at the PR head (D8, D20) | none | `dark-factory/approval` |
| `platform/merge` | Re-reads the PR head, requires a platform-App check for every rendered step and every `requiredSteps` entry, merges, closes the issue (D7) | none | none (merges) |

### 3.4 Component properties

These are the only things a team sets.

| Property | Type | Default | Config key |
|---|---|---|---|
| `repositories` | `{owner, names}` — **one owner** | required | — (one App installation per factory, D15) |
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

A step's own settings go in its `config` and reach it as `DF_CONFIG`, checked against the step's
schema at render; `factoryPolicy` can bound them (D22).

**Deliberately not team properties:**

- **Environment values** — account, region, Bifrost, runtime class — come from the platform (D19).
- **Sandbox settings** — coder image, pool size, Nitro throttles — belong to the platform-owned
  sandboxes Application and are set in the onboarding entry (§3.2, D20). The coder image runs
  with the coder token mounted, so it must be digest-pinned from `allowedRegistries` (D7).
- **GitHub App installation IDs** live in the onboarding record (D15).
- **The secrets-store prefix** is derived as `<cluster>/dark-factory/<ns>/<name>`, with no
  override. External Secrets' identity is per cluster, so a settable prefix would let a team
  sync another factory's secrets into its own namespace.

**Why the coder pool defaults to 0.** The claim injects env (`20-workflowtemplate-df-run.yaml:226`),
and operator v0.5.1 skips the warm pool for any claim with env
(`sandboxclaim_controller.go:1626-1627`). A warm member would never be used — and on Nitro it
holds a node while idle. Delivering run inputs without claim env is a follow-up (§8).

### 3.5 How the factory's isolation finds its sandboxes, without changing #151

D13's network policy must select the factory's sandbox pods. #151's `type: sandbox` labels every
pod `agent-sandbox.io/role: "session"` (`sandbox.cue` lines 334–342 at `90ae55b6`) and has no
parameter to change it — deliberately, because `role: coder` selects the hub's coder egress
rules, which should not apply to unrelated workloads. So the factory cannot select its pods by
`role: coder`.

**Chosen: select by onboarded namespace and component name.** Two labels already identify a
factory sandbox, and only the platform can set either:

- **The namespace label `dark-factory.io/onboarded: "true"`**, which onboarding sets on every
  factory namespace (D20). Teams have no write access to namespaces.
- **The pod label `app.kubernetes.io/name`**, which `type: sandbox` sets to the component's name
  (`sandbox.cue` line 335). The component names are fixed by the contract:
  `dark-factory-coder`, `dark-factory-holdout-install`, `dark-factory-holdout-test`. The clone
  pod is the component's own, named `dark-factory-clone`.

D13's policy selects pods matching **both**. A team elsewhere can name its own sandbox
`dark-factory-coder`, but its namespace lacks the onboarding label; inside a factory namespace,
a team cannot create pods at all (D20). The runner also stamps holdout's launches with
`dark-factory.io/launch: install | holdout-test`, which the stricter rules key on (D13).

Why it is safe: the policy's two inputs are controlled by the platform alone — the namespace
label by onboarding, the pod label by the platform-owned `dark-factory-sandboxes` Application
and the platform runner. It needs no change to #151, and the pods stay `session`, as #151
intended.

To confirm (§7): EKS's `ClusterNetworkPolicy` accepts a subject that combines a namespace
selector with a pod selector, as upstream AdminNetworkPolicy does. If it does not, the policy
selects on the namespace label alone — every pod in a factory namespace is a factory pod — and
the per-component rules key on the pod labels within it.

| Option | Verdict |
|---|---|
| **Onboarded namespace + component name** (**chosen**) | No change to #151; both labels are platform-controlled; uses labels `type: sandbox` already sets |
| **A platform-settable `role` parameter in #151** | `role: *"session" \| "coder" \| "step"`, plus a cluster-wide `ValidatingAdmissionPolicy` admitting a non-`session` role only from the platform's GitOps identity (a team that could set `role: coder` could put any workload under the factory's rules), plus a matrix row. Explicit and self-describing, but a change and a new admission rule in #151. The fallback if the chosen option fails §7 |
| A generic pod-labels parameter in #151 | Also a #151 change, and still needs a platform-only admission rule |
| Copy `sandbox.cue` into the factory | No #151 change, but copies drift |
| Make D13 select `session` | Applies the factory's rules to every interactive session in the cluster |

---

## 4. Decisions

### D1. The platform owns the merge decision and the steps; teams own the process

The process — stages, order, where humans approve, what happens on failure — is not a security
control, because anyone who can create a Workflow can bypass a fixed shape. What holds is the
merge decision and the ruleset (D7, D20). So the process belongs to the team.

Some steps, though, *are* controls:

| Step | Why platform-owned | Team-ownable later? |
|---|---|---|
| `merge` | It *is* the merge decision | **Never** |
| `approval` | It decides that a human approved | **Never** |
| `holdout` | It is the only reader of the hidden scenarios | **Never** (the evaluator, possibly — §8) |
| `security-scan` | Policy may require its verdict | **Never** |
| `changeset` | Feeds `skipWhen`; a wrong answer skips gates | Once the platform evaluates skips from a team-supplied path list (§8) |
| Contract tests, licence scans, compliance tools | Report a verdict; cannot weaken anything else | **Yes, later** (§8). In v1 they gate through `platform/ci-check` (D18) |

**The rule for what stays reserved:** a step is reserved only if a team version could weaken a
security control — not merely because it could be wrong. In v1 every step is platform-built
anyway; this rule decides what the later version may open up.

**Costs.** A check that is not already a GitHub check cannot join the process in v1. And a
team can compose a process that passes policy while verifying very little (§5).

### D2. One contract for every platform step

| Option | Verdict |
|---|---|
| **Env in, result file out, the same for every step** (**chosen**) | One shape to test; the renderer needs no per-step knowledge; it is also what team steps would build against later |
| Argo parameters per step | Every step's template becomes its own interface |
| A step SDK or base image | Ties every step to one language |

The contract does not give a step's own logic credentials or verdict posting; the step's
template provides both (§3.1).

**Testing.** A local runner, `df-step-test`, runs a step image with a `DF_*` environment and a
local checkout, then checks the result file. A **process** change is tested by running it on a
factory in a dev environment against a scratch repo.

**Cost.** None while internal. Once teams build against it (§8), a breaking change becomes a
new *contract* version (D9).

### D3. Step addressing

A step is addressed `<tier>/<name>`. v1 has one tier, `platform/`; any other tier fails render.
The shape exists so `<team>/<name>` and `shared/<name>` can be added later without changing the
developer API. The platform publishes the `platform/` catalog — name, version, `config` keys,
docs — as the §3.3 table and as a ConfigMap status can read.

### D4. Holdout's launches: sandboxes created from #151's templates

Holdout runs the coder's output against the hidden scenarios in two **launches**: install, then
test (D17). The platform-owned **runner** (`df-step-runner-v1`) starts them, reads their
results and hands them to `platform/holdout`'s judge.

| Option | Verdict |
|---|---|
| **A `Sandbox` created from a `type: sandbox` template** (**chosen**) | Isolation is #151's, unchanged; the runner adds only run-specific fields (§3.2) |
| Claim from a `type: sandbox` pool | Simplest, but a claim cannot carry the per-run volume or the per-repo scenario key |
| A hardened ordinary pod | A second isolation setup to maintain; namespaced NetworkPolicy does not reliably apply to sandbox pods on EKS VPC-CNI (D13) |

**Launch shape.** A launch never runs as an Argo task: Argo's `wait` container needs a
ServiceAccount token, which inside the sandbox would reach the API server.

| Concern | How |
|---|---|
| Launch | The runner (a platform Argo task, SA `dark-factory`) creates a `Sandbox` from the #151-rendered template. Runtime class, `networkIsolated`, the token-less SA `dark-factory-executor`, the `app.kubernetes.io/name` label D13 selects on, and every other isolation field come from the template. The runner adds only: the workspace, the scenario key, `restartPolicy: Never` (or a finished launch restarts and never reaches `Succeeded`), `spec.shutdownTime` and the `dark-factory.io/launch` label |
| Result | `terminationMessagePath: $DF_RESULT`; the runner reads it from the pod's status. The launch needs no token or API access. Unparseable means `failure` (§3.1) |
| Workspace | A per-run volume (below). The launch holds no credential and needs no network to get its source. Scratch space is #151's size-limited `emptyDir` |
| Logs | The install launch's log is linked from holdout's check-run. The **test launch's log is never surfaced** (§3.1, D17) |

**Workspace: a per-run volume.** The clone cannot happen inside a launch: under the default
`stepEgress: none` the install launch has DNS only, and the test launch has no network at all.
So for each holdout run, the runner:

1. creates a PVC named from the **Workflow's UID and the holdout node's ID**, so a later run of
   the same issue never collides with or mounts it. It is `volumeMode: Filesystem`,
   `accessModes: [ReadWriteOncePod]`, from the platform's workspace StorageClass (D19: EBS gp3,
   `WaitForFirstConsumer`, and on Auto Mode `allowedTopologies` for
   `eks.amazonaws.com/compute-type: auto`, or it is never provisioned), sized by
   `factoryPolicy.workspaceSize`;
2. runs a short **clone pod** (below), which fills the volume and exits;
3. runs the install launch, then the test launch, each mounting the volume. The volume pins
   them to the clone pod's availability zone;
4. **deletes the PVC** after the last launch. The Workflow also owns it, as a backstop.

The **clone pod** is platform code and holds the only credential in this chain:

| Concern | Setting |
|---|---|
| Identity | ServiceAccount `dark-factory-clone`, `automountServiceAccountToken: false` |
| Labels | `app.kubernetes.io/name: dark-factory-clone`. D13 gives it DNS and public port 443, nothing else |
| Credential | The **reader** token (contents:read, D15), as a file, supplied through a git credential helper. It is never written into the remote URL, `.git/config` or the volume, so a launch cannot find it |
| Clone | The branch only, `--no-recurse-submodules`, `GIT_LFS_SKIP_SMUDGE=1` |

An EBS volume attaches to one pod at a time; on Nitro it is attached to the sandbox as a block
device, and AWS documents `ReadWriteOncePod` for it. The clone pod and the two launches use the
volume one after another, never at once, so they share one volume: the installed tree is
already there when the test launch starts. The test launch mounts it **read-only**, so nothing
it writes can reach a later launch. Nitro cannot resize an attached volume, so the size is
fixed per run.

Documented: Nitro sandboxes mount EBS through the EBS CSI driver (on Auto Mode,
`ebs.csi.eks.amazonaws.com`), and EFS is unavailable under `networkIsolated` — hence EBS. Not
documented, and so a §7 go/no-go: EBS under `networkIsolated`, and EBS on Kata.

| Option | Verdict |
|---|---|
| **Per-run PVC, filled by a platform clone pod** (**chosen**) | Works with no network in the launch; no credential in a launch; deleted with the run |
| Runner streams a tarball over `exec` | No volumes, but the runner needs `pods/exec`, data flows through the API server, and `confidential` launches are ruled out. The fallback if a runtime fails the go/no-go |
| Clone inside the launch | Needs GitHub egress in the launch |
| An object store | A store and credentials to run; D16 bars AWS identity in a factory |

**Costs.** Two sandbox boots, a volume, a clone pod and three attaches per holdout run; one
availability zone per run; and quota for PVCs (D20).

### D5. The process is rendered to a per-namespace WorkflowTemplate

| Option | Verdict |
|---|---|
| **The component renders `process` into `dark-factory-process`; each task is a `templateRef` with `clusterScope: true`** (**chosen**) | KubeVela renders and reconciles it, so a process change is a normal Application update. Readable and diffable; holds no step code |
| One cluster-scoped template that interprets the process at run time | Nothing per factory, but it cannot express per-stage `onFail` cleanly, debugging means reading the interpreter, and the UI shows a generic loop |
| KubeVela's Application `workflow` field | Runs per deployment of the Application, not per issue |

A stage's tasks depend on the previous stage's; steps within a stage run in parallel. Every
task references a platform step at the factory's `catalogVersion`.

**Costs.** A per-factory object holds the shape, so a process bug can be unique to one factory.
The renderer caps stages and steps per stage (D7) to stay well inside object-size limits.

### D6. Presets

`process.preset` names a platform-defined process that the component expands before applying
overrides. `standard` is the README's Level 3 process — implement; verify with holdout,
security-scan and the DevOps Agent (via `platform/ci-check`); approve; merge — and the
recommended start. A team can add catalog steps without leaving the preset:

```yaml
process:
  preset: standard
  approvals: 1
  maxIterations: 3
  addSteps:
    verify: [{use: platform/ci-check, config: {check: "ci/contract-tests"}}]
```

**Presets describe processes, not languages.** `PROFILES.md` rejected per-language profiles
because the coder detects toolchains itself. A preset describes who approves and what gates,
which no repo inspection can infer. The preset list must not grow language-shaped entries.

**Cost:** a process can come from a preset, `addSteps` or explicit `stages`, so status reports
the **expanded** process (D20).

### D7. Guardrails: platform minimums at three enforcement points

```yaml
factoryPolicy:
  requiredSteps: [platform/security-scan]
  minApprovals: 1
  allowedRegistries: ["<acct>.dkr.ecr.<region>.amazonaws.com/"]   # for the coder image
  stepEgress: none          # none | public — holdout's install launch egress (D13)
  workspaceSize: 10Gi       # holdout's per-run volume (D4)
  maxStages: 12
  maxStepsPerStage: 8
```

1. **Render.** A non-compliant process fails the Application, naming the broken rule. Checks
   `requiredSteps`, `minApprovals`, size caps, that every `use:` is a `platform/` step, and that
   `repositories` has one owner. Onboarding applies `allowedRegistries` and digest pinning to
   the coder image.
2. **Merge.** `platform/merge` requires the verdict of every step in the rendered process
   **and** every `requiredSteps` entry, so deleting a required step does not make a PR
   mergeable.
3. **GitHub.** The ruleset requires the `dark-factory/*` checks from the platform App.

Each covers the others' gaps: render gives the clear error but only covers the component path;
merge catches a hand-written Workflow that skipped a step; the ruleset catches one that skipped
merge.

**Verdicts are recognised by App, not name.** The platform App posts every `dark-factory/<name>`
check, and merge and the ruleset both check the App (§3.1), so a same-named status from another
token counts for nothing. When team tiers arrive (§8), `requiredSteps` must stay limited to
`platform/` steps, or a team could satisfy a required gate with its own stub.

**Cost.** Policy lives in Helm values per environment, out of the team's sight, so one
Application can be valid on one cluster and rejected on another. That is intended (dev versus
production), but it makes promotion something to test.

### D8. Failure handling, fast paths and one iteration budget

| `onFail` | Behaviour |
|---|---|
| `stop` (default) | The run fails; checks stay red; the PR is not mergeable |
| `revise` | The failing steps' `summary` and log tail go back to the coder, using one iteration |
| `notify` | Post to the PR and continue. For advisory checks |

Every step's failure feeds `revise` the same way, because every step returns the same contract
(D2).

**One iteration budget.** `onFail: revise` and human PR comments share one `maxIterations`,
counted per PR. Separate counters would let `revise` on several stages plus comments loop
without a real cap. A round that would exceed it posts a "human needed" comment naming the
stages involved, and stops.

**Fast paths.** `skipWhen: {pathsOnly: [...]}` on a stage skips it when **every** changed path
matches. `platform/changeset` runs early, compares the changed paths with each stage's list and
emits `skip.<stage>: "true"|"false"` as `outputs`, which the stage's `when:` reads (D22). "All
paths match", never "any path matches", so a PR touching `docs/` and one line of `src/` still
runs the stage.

**Concurrency.** `maxConcurrentRuns` is an Argo semaphore per factory. A per-issue mutex, keyed
on the run key (D21), makes a revision run wait for its original instead of both claiming a
sandbox.

**Human approval ends the run.** `platform/approval` is a stage boundary, not a wait: the run
posts `dark-factory/approval=pending`, releases its sandbox, mutex and concurrency slot, and
ends. A GitHub review event starts a **continuation run** from the next stage, with the same
run key. It re-checks every earlier verdict at the PR head, so a push after approval
invalidates it. The hub chart's merge workflow already works this way. Stages after an approval
are rendered as a second entry point of `dark-factory-process` (D5).

**Cost:** a process with N approvals is N+1 runs, which status shows as one (D20).

### D9. Versioning

Each platform step is a template `df-step-<name>-<version>` that pins an image digest. A
factory pins `catalogVersion` (or one step's `version`), and every reference renders at it, so
a run never mixes versions.

- A **compatible fix** ships as a new digest inside the same template. Factories pick it up
  without changing anything.
- An **incompatible change** (a new required `config` key, changed verdict meaning) ships as a
  new version of **that step**.
- Each run records the digests it used, and status reports them (D20).

The step contract is versioned separately (D2).

### D10. Platform steps are images

Platform step logic comes from the `dark-factory` chart (`merge.js`, `iterate.js`,
`security-wait.js`, `holdout/evaluate.js`), which the chart mounts from a ConfigMap. A
ConfigMap can only be **mounted** in its own namespace, so that does not work across factories.
(Reading a ConfigMap by name, which D11 uses, is a different path and does work.)

| Option | Verdict |
|---|---|
| **One image per platform step, digest-pinned in its template** (**chosen**) | One way to run and test every step, and the shape a team step would have later |
| Scripts embedded in templates as Argo artifacts | No image build, but it fuses code into templates and adds symlinks and build plumbing |
| One image for all steps | Fewer artifacts, but a change to `merge` re-releases `holdout`, and per-step versions stop meaning anything |

**Why images:** steps can later move to teams as a catalog change rather than a rewrite (D1);
`df-step-test` and §7's suite cover every step the same way; and it avoids `.Files.Get`,
artifacts and symlinked scripts.

**Drift is prevented by the digest.** Each template pins its image by digest, so
`df-step-merge-v1` always means one known image, and only a reviewed platform commit changes
it. Teams cannot change it: the digest lives in a cluster-scoped, platform-owned template.

**Publishing.** The design only needs templates to pin an immutable digest. The recommendation:
build once into a platform account and **mirror** to each hub; ECR replication keeps the digest,
whereas rebuilding per account would not. Outside our accounts the shipped digests are
placeholders that an operator re-pins through chart values; a placeholder left in place fails
render, like #151's guard, so a missed re-pin is loud.

**Costs.** A build-and-publish pipeline, and several small images instead of one.

### D11. Factory-wide settings in a ConfigMap

Step parameters default from `dark-factory-config` using `valueFrom.configMapKeyRef`, which
Argo resolves in the **run's** namespace, so one cluster-scoped template serves every factory.
Keys are only genuinely factory-wide settings: `coder-engine`, `bifrost-url`,
`max-iterations`, `max-concurrent-runs`, `run-timeout-seconds`. Per-step settings travel in
`DF_CONFIG`. (The alternative, the Sensor passing every setting as an argument, makes every
trigger carry everything.) There are no sandbox TTL settings: sandbox lifetime is `shutdownTime`,
set per claim and per launch (§3.2).

Settings are read when a run starts. The ConfigMap needs the label
`workflows.argoproj.io/configmap-type: Parameter`, and the controller's `argo-cluster-role` must
grant `configmaps get/list/watch` (checked on `dora-showcase` with
`kubectl get clusterrole argo-cluster-role -o yaml`; the implementation re-checks each hub).

One behaviour is still unobserved: that these defaults resolve in the run's namespace when the
template is cluster-scoped. §7 makes it a go/no-go with a fallback.

A step a team does not want is simply absent from its process, so there are no `*-enabled` keys.

### D12. Fixed names; one factory per namespace

Fixed names keep templates readable, and some fields (the template-level executor SA,
ConfigMap references used to resolve parameters) cannot safely be templated. The namespace is
Kubernetes' boundary for RBAC, Secrets, NetworkPolicy and quota; two factories in one would
share credentials, blast radius and sandboxes.

The workflow ServiceAccount is `dark-factory`, not `context.name` as
[`.kiro/steering/oam-authoring.md` §3](../../.kiro/steering/oam-authoring.md) prescribes ("A
component that runs a workload owns its ServiceAccount, named `context.name`"). It must never
gain an AWS identity (D16), so not being an identity-trait anchor is the point.

### D13. Network isolation keyed on platform-controlled labels

On EKS VPC-CNI an ordinary NetworkPolicy does not reliably apply to sandbox pods, so the real
isolation is an Admin-tier `ClusterNetworkPolicy`. It selects pods in namespaces labelled
`dark-factory.io/onboarded` whose `app.kubernetes.io/name` is one of the fixed component names
(§3.5) — every factory's coder, holdout launches and clone pods, automatically. Only the
platform sets those labels, so coverage never depends on an image or a team. A per-factory
policy would mean a team Application owning cluster-wide network policy.

Namespaced NetworkPolicy for the sandboxes is #151's; the factory adds none that could conflict.

**The rules in the policy**

| Rule | Applies to | Effect |
|---|---|---|
| Deny private ranges | All factory pods | Deny the cluster's service and pod ranges, the VPC's private ranges and the instance metadata endpoint. The service range must be listed explicitly, because VPC-CNI matches egress before Service translation |
| Allow DNS | All factory pods except holdout's test launch | Allow port 53 only to the cluster resolver the sandbox's DNS policy uses — the kube-dns ClusterIP `/32` (§7 confirms it for #151's templates) |
| Allow the coder's egress | `dark-factory-coder` | TCP 443 to any address not denied above (GitHub, package registries), and the Bifrost ClusterIP `/32` (D19). This replaces the hub chart's `role: coder` rules, which no longer select factory pods (§3.5) |
| Allow public HTTPS | `dark-factory-clone`, and holdout's install launch where `stepEgress: public` (runner label `agent-sandbox.io/egress: public`) | TCP 443 to any address not denied above |
| Deny all for holdout's test launch | Label `dark-factory.io/launch: holdout-test` | Deny **all** egress, port 53 included, at a higher priority than the DNS allow |
| Ingress | All factory sandboxes | Allow only from the pod's own namespace, matching #151 |

Everything else is denied.

**Why holdout's test launch gets no DNS either.** It runs coder-written code with the scenarios
mounted, and DNS queries alone can carry data out. On Nitro the launch is also
`networkIsolated`, so it has no network device at all.

**The start-up window.** In VPC-CNI's default `standard` mode, a new pod can send traffic until
its policies are programmed (observed in `30-networkpolicy.yaml:48`). Strict mode would close
this but is cluster-wide and needs a policy for every pod. Instead, on Kata the test launch
starts with an init container that probes a public IP and waits until the probe is refused; the
test container — the only one that mounts the scenarios — starts only after that. On Nitro,
`networkIsolated` leaves no window.

**Where the rules are enforced.** These are CIDR rules, enforced by `ClusterNetworkPolicy` with
VPC-CNI 1.21.1 or later. EKS documents network policy for **EC2 Linux nodes**: that covers the
Kata pools (self-managed Karpenter) and, per the EKS security-model page for Nitro Sandboxes,
Nitro on Auto Mode. Each runtime must pass §7's per-runtime test before `stepEgress: public` is
enabled on a cluster.

**Why not per-host allowlists yet.** `domainNames` rules are enforced only on Auto Mode nodes;
elsewhere they are accepted and silently ignored. The factory assumes #151's default — Nitro on
Auto Mode — with Kata on Karpenter as the alternative, where such a rule would be ignored. On
Nitro, per-host egress becomes an option once §7 confirms it works for a guest (§8).

| Option | Verdict |
|---|---|
| **CIDR rules, public-or-none** (**chosen**) | Enforced on both runtimes, confirmed per runtime in §7; nothing new to run |
| `domainNames` rules | Ignored on Kata. A later option for Nitro clusters |
| A forward proxy | Works everywhere, but a new service and failure point |
| No egress at all | Most dependency installs need a package registry |

**Cost:** with `stepEgress: public`, coder-written install scripts can send the source to any
public host (§5). An environment that needs a host allowlist adds one at the VPC (AWS Network
Firewall or Route 53 Resolver DNS Firewall).

**Two Admin policies must not share a priority.** This policy and the hub's existing one both
default to `adminPriority: 10`; if the controller requires unique priorities, one is rejected.
This one takes `adminPriority + 1`, to be confirmed on a live cluster.

### D14. Event plumbing per namespace

Each factory namespace has its own EventBus, EventSource, Sensor and Ingress. Each receives only
its own repos' webhooks under its own secret, and a broken factory cannot affect another. One
central EventSource would be fewer components but a single point of failure holding every
factory's webhook secrets.

**Cost:** a JetStream EventBus per namespace (3 small pods), shareable with
`eventBus.create: false`. Webhook paths include the namespace (`/dark-factory/<ns>/<name>`), so
factories share one load-balancer host.

### D15. One platform GitHub App; four short-lived tokens per factory

The platform registers **one GitHub App per environment**, and each team installs it on its
repos. The App's private key stays **only in a platform namespace**, because whoever holds it can
mint tokens for every repo the App is installed on. The App does not have
`administration: write`, so no token from it can weaken the ruleset (D20).

**The four tokens**

| Token | Used by | Can |
|---|---|---|
| coder | The coder sandbox (file mode 0400, never in env) | Read issues; write contents and PRs. **No checks** — `platform/code` posts for it |
| orchestrator | **Platform steps only** | Also `checks: write` and issues write. The only token that can post a verdict merge accepts |
| events | The EventSource only | Write repository webhooks, to register them |
| reader | The clone pod only (D4) — never a sandbox | Read contents |

**Who can merge.** GitHub has no separate merge permission: any token with contents write (the
coder's and the orchestrator's) can *call* merge. Whether it *succeeds* is the ruleset's call,
and the ruleset requires every `dark-factory/*` check from the platform App, approval included,
to be green at the PR head. The coder token cannot create those checks, so it cannot merge what
a human has not approved and every gate has not passed. `platform/merge` just makes the call
once they have.

**How tokens are made: External Secrets, no new service.** Everything that touches the key
stays in the platform namespace:

1. Per factory and role, a **`GithubAccessToken` generator** in the platform namespace. It uses
   the App key, which must be in the same namespace, and carries the factory's installation ID,
   repo names and the role's permissions. The token's scope is set here.
2. Per factory, a **`SecretStore` using External Secrets' Kubernetes provider**, in the platform
   namespace, with `remoteNamespace: dark-factory-<team>`. For this provider the "remote store"
   is the Kubernetes API: pushing to it writes an ordinary Secret in that namespace. Its
   ServiceAccount's Role in the factory namespace allows only `get` and `update` on the four
   `dark-factory-github-*` Secrets. RBAC cannot limit `create` by name, so **onboarding creates
   the four Secrets empty** and the Role never needs `create`.
3. Per factory and role, a **`PushSecret`** in the platform namespace, with
   `selector.generatorRef` set to that role's generator and `refreshInterval: 20m`. Each refresh
   mints a token and writes it into `dark-factory-github-<role>`. (The default of one hour is a
   token's whole life; 20 minutes leaves at least 40 minutes of validity, which covers a
   30-minute run.)

So the chain is generator → `PushSecret` → Kubernetes-provider `SecretStore` → an ordinary
Secret in the factory namespace. The factory namespace holds only the four Secrets — no key, no
generator, nothing that pulls — and no factory pod holds a Kubernetes token.

**What limits a factory to its repos.** Onboarding creates all of these objects in the platform
namespace, where teams have no access. Together they are the factory's **onboarding record**:
the repos and the one installation ID its tokens may name. A factory covers **one repo owner**,
because an installation covers one owner; render rejects a second. Editing `repositories` in
the Application cannot widen a token; a repo missing from the record gets no token, and status
says so (D20).

**Why minting happens in the platform namespace.** External Secrets' generators, including
`ClusterGenerator`, need the key in the same namespace as the object they fill. Minting
directly into a factory namespace would put the key in every factory.

| Option | Verdict |
|---|---|
| **One App; External Secrets mints in the platform namespace and pushes through a scoped Role** (**chosen**) | GitHub enforces who posted a verdict; tokens are short-lived and scoped; no machine users; the key stays in one place; nothing new to run |
| The same, minted by a token service or CronJob | Same security, but platform code to run. The fallback if a hub's External Secrets cannot do the above |
| A generator in each factory namespace | One hop, but the key sits in every factory |
| Fine-grained PATs | No App, but GitHub checks names only, tokens are long-lived, and the orchestrator needs a machine user |
| One App per factory | Smaller blast radius, but the key sits in a namespace that runs untrusted code — a coder escape could forge its own factory's verdicts — and every team runs its own App |

**Costs:** one more App per repo for teams; a high-value private key; nine External Secrets
objects per factory (four generators, four `PushSecret`s, one `SecretStore`); the External
Secrets controller can read every token (as it already can every Secret it manages); if minting
stops, tokens expire within the hour and runs fail closed.

### D16. No AWS identity anywhere in a factory

No factory ServiceAccount has IAM, and nothing may attach an identity trait. The coder and
holdout's launches run untrusted code in the factory namespace, so having no AWS credentials
there at all is simpler and stronger than scoping them per step. Holdout's launches see no
GitHub credential either: the reader token is only in the clone pod (D4).

`platform/security-scan` waits for the Security Agent **GitHub App** and copies its verdict,
instead of calling AWS APIs; the chart's IRSA-plus-S3 mode is not ported. A scoped identity for
the Security Agent's API path was considered and rejected for v1: it would still sit on a
ServiceAccount in a namespace that runs untrusted code, and it needs account-specific values a
public chart must not default. Revisit if the App path proves unworkable.

**Cost:** `security-scan` needs the App installed per repo, and a future `deploy` step is harder
than with IRSA.

### D17. Holdout scenarios: a synced Secret

Scenarios live in `dark-factory-holdout`, one key per repo (`<owner>-<repo>.json`), synced from
the secrets store. They are the factory's one piece of confidential team data, so they belong in
the secrets store, not a hand-applied ConfigMap outside GitOps and status. They stay out of the
Application, because an Application in the target repo would let the coder read them. Only
holdout's test launch ever mounts them — which is why holdout stays platform-owned (D1). A repo
with no scenarios fails (`NOT VERIFIED`) while `requireScenarios` is true, the default.

**How holdout runs**

1. **Install launch** — the workspace, no scenarios. Public egress only where
   `stepEgress: public`. Under the default `none` it has DNS only, so dependencies must be
   vendored in the repo or in the test image; a repo that needs a registry fails holdout with a
   status naming `stepEgress`, rather than passing untested.
2. **Test launch** — the same workspace, read-only, and **only this repo's scenario key** (a
   Secret volume `items:` entry for `<owner>-<repo>`). **No network at all, DNS included**
   (D13). It never starts inside the start-up window: on Nitro it is `networkIsolated`; on Kata
   an init container waits until egress is refused before the test container starts.
3. **Judge and verdict** — in the platform step. It reads the test launch's results from the
   runner, has the judge score them, and posts **only the judge's counts** — never the test
   launch's log or `summary` (§3.1).

The coder's output never shares a pod with a credential or another repo's scenarios.

**Cost:** two launches per holdout run, and the evaluator splits into a test image and a judge.

### D18. `ci-check`: gating on checks that run elsewhere

`platform/ci-check` waits, with a time limit, for a named GitHub check and makes merge require
it:

```yaml
- {use: platform/ci-check, config: {check: "ci/unit-tests"}}
```

In v1 this is how a team brings its **own tooling** into the process. Anything it already runs as
a GitHub check — contract tests, a licence scanner, a compliance tool — gates the factory
through `ci-check`, and a failure feeds `onFail: revise` like any other step. Running tooling
*inside* the factory is the later team-step version (§8). A timeout is a failure.

The DevOps Agent is wired this way: `standard` includes
`{use: platform/ci-check, config: {check: "aws-devops-agent/release-readiness-review"}}`. On
timeout, the status names the likely cause (repo not connected to the Agent Space), so a missing
connection fails loudly. The chart's `devopsAgent.gate: label` mode is dropped; a team using the
coding-agent plugin wires its own check.

### D19. Platform-supplied environment values

Developers never set account, region, cluster name, webhook host or Bifrost's address. These
flow from the cluster secret's annotations, through the addon registry, into Helm globals
(`awsAccountId`, `awsRegion`, `clusterName`, `bifrostUrl`, `bifrostClusterIp`, `ingressDomain`,
`workspaceStorageClass`) and on into CUE defaults.

**Bifrost** is one cluster value, as in #151: a pinned ClusterIP by default, overridable with
the cluster's `bifrost_url` annotation. The ClusterIP exists because the sandbox operator can
give sandboxes `dnsPolicy: None`; since the factory's sandboxes are #151's, they reach Bifrost
exactly as any `type: sandbox` does. The ClusterIP is allowed by a `/32` egress rule (VPC-CNI
matches egress on the pre-translation ClusterIP) and must match in three places: the bifrost
chart's `staticService.clusterIP`, agent-sandbox's `networkPolicy.bifrostClusterIp`, and this
value.

**`webhook.host`** defaults to `ingressDomain`; factories are told apart by D14's
`/dark-factory/<ns>/<name>` path, as the hub factory does today (`registry/sandbox.yaml`). A
per-factory host would sit two labels below the domain, which a single-level wildcard
certificate does not cover. Override it only with a host the cluster's certificate matches.

### D20. Status, onboarding and tenancy

**Status.** A factory that silently fails because a secret is missing is an abstraction leak.
`customStatus` and `healthPolicy` report:

- the **expanded** process: stages, steps, resolved versions and digests;
- each prerequisite: the App installation and the four tokens, scenarios per repo, the
  `dark-factory-sandboxes` Application's health (which is #151's own pool health), webhook
  registration;
- which `factoryPolicy` applied.

**Onboarding.** The namespace is the tenant boundary, so the team must not administer it.
Onboarding is an entry in the `dark-factory-templates` chart's `onboarding.factories` values,
reviewed and synced like any platform change. One entry per factory: namespace, repo owner and
names, installation ID, coder image, pool size and throttles. It creates:

- in the platform namespace, the factory's **onboarding record**: the External Secrets objects
  that mint and push its tokens (D15);
- the namespace `dark-factory-<team>`, labelled `dark-factory.io/onboarded: "true"` — the label D13's isolation selects on (§3.5), which teams cannot set or remove — with:
  - the four empty token Secrets and the push Role (D15);
  - the platform-owned `dark-factory-sandboxes` Application (§3.2), applied by the platform's
    GitOps identity;
  - a **team Role** that may apply and read its `Application` and read Workflows, pods and logs
    — and write nothing else: no Secrets, Pods, Workflows or RBAC;
  - **one admission policy** on Applications (below);
  - a **ResourceQuota** for the coder pool, `maxConcurrentRuns` coder claims, holdout launches
    and their PVCs (count and storage), **including each pod's runtime overhead**, which counts
    toward quota: on Nitro, 256Mi (isolated) or 512Mi (confidential) plus one
    `eks.amazonaws.com/nitro-sandbox` slot per pod.

**The admission policy — the tenancy control.** Permission to apply an Application is not
narrow by itself: vela-core applies resources with its own identity (unless
`authentication.enabled`, default `false`), and `k8s-objects`, `ref-objects`, the `json-patch`
and `json-merge-patch` traits, policies and workflow steps can each create or rewrite objects
the team Role forbids. So in factory namespaces a `ValidatingAdmissionPolicy`
(`failurePolicy: Fail`, CREATE and UPDATE) enforces an **allowlist**:

- an Application's `spec` may contain **only `components`**;
- each component may contain **only `name`, `type: dark-factory`** (no `@version`) **and
  `properties`**;
- a second binding denies any `*Definition` kind in the namespace, because KubeVela looks up
  `type` in the Application's own namespace first, so a local `dark-factory` definition would
  win.

Because it is an allowlist, fields vela adds later (such as `spec.workflow.ref` or vela 1.12's
`spec.sources`) are rejected by default. The platform's own `dark-factory-sandboxes`
Application is exempt by the platform's GitOps identity, which the team cannot act as.

With that, vela can create only what the platform's `dark-factory` definition renders. That
definition exposes **no pod fields** — no raw pod spec, volumes, env-from-secret, labels or
image — so no team-controlled path produces a pod that mounts a factory token. This is the only
tenancy control; what remains is a cluster administrator granting a team more RBAC (§5).

**GitOps is recommended, not a substitute.** A team should keep its Application in a repo ArgoCD
syncs, with its own review. Platform review is optional and does not replace the admission rule:
the risk is not extra resources (quota caps those) but a few lines — a `json-patch` trait, a
`k8s-objects` pod — that mount the orchestrator token and forge every gate. That is easy to miss
in review, and mandatory platform review would put the platform back in every process change
(D1). ArgoCD also applies with its own identity, and an AppProject limited to the `Application`
kind cannot see inside one.

**The ruleset — the hard barrier on merging.** GitHub enforces a ruleset on every push and merge,
from the API, UI or git, so it is what stops a merge no factory component should make. It holds
only if the default branch's ruleset has all of:

| Setting | Required value | Why |
|---|---|---|
| Required status checks | Every `dark-factory/*` check of the rendered process and `requiredSteps`, each pinned to the **platform App** | A same-named check from anyone else does not count (§3.1) |
| Required approvals | At least `process.approvals` and `factoryPolicy.minApprovals`; stale approvals dismissed on push; code-owner review where the team uses CODEOWNERS | `platform/approval` reads GitHub's `reviewDecision`, which GitHub computes from these rules, so they cannot disagree. Limit approvers to a team with CODEOWNERS |
| Force pushes, deletions | Blocked | History cannot be rewritten around the checks |
| Enforcement | **Active** | "Evaluate" only logs |
| Bypass list | **Empty** | Any bypass actor skips every rule |
| Level | **Organization ruleset** (recommended) | Repo admins can change a repo ruleset; only organization owners can change an organization one |

Organization rulesets on private repos need GitHub Team or Enterprise. With only a repo ruleset,
the team's repo admins can still turn it off — the check below notices (§5).

**The ruleset is checked, not assumed.** At startup and before every merge, the factory reads the
default branch's rules with the orchestrator token and reports in status whether the App is
installed and every row above holds. Any gap fails closed: runs proceed, merge refuses, and
status names the missing setting.

**Cost:** status needs state from objects the component does not own, so its expressions get
long. A reconciler would be better (§8).

### D21. Smaller choices

| Choice | Reason |
|---|---|
| Run key is `<owner>-<repo>-<issue-number>`, sanitized and hashed | The number is in every payload, including a revision run found from a PR, which `issue.id` is not. The bare number is unique only per repo, so the key includes owner and repo. Repo names can contain `_` and `.`, which Kubernetes names reject, and joining with `-` is ambiguous (`acme-api/web` vs `acme/api-web`). So: lowercase, every character outside `[a-z0-9-]` becomes `-`, and an 8-character hash of the exact `owner/repo#number` is **always** appended, truncating the readable part to fit 63 characters |
| `synchronization.mutexes` (list form) | Argo v4 removed `mutex`: `argo lint` on v4.1.4 rejects it; the list form works on v3.6+ |
| No issue title in the claim | Raw issue text broke the claim manifest on quotes; the coder reads the title itself |
| Revision notes base64-encoded | Arbitrary markdown cannot break the claim manifest |
| Deterministic Workflow names per event | GitHub redelivers webhooks; duplicates collide (`AlreadyExists`) instead of racing |
| Addon gate `enable_dark_factory_templates`, which requires `enable_agent_sandbox` | Not `enable_dark_factory`: since #151 that label also starts the agent-sandbox chart's own coder pool, which would run beside each factory's pool. The operator and CRDs come from `enable_agent_sandbox` (`sandbox.enabled`) |


### D22. Customization now, team logic later: keeping both doors open

Teams need two kinds of control over platform steps. v1 delivers the first and is built so the
second can be added later **without changing the developer API or the platform steps**.

**1. Parameters, in v1.** Every platform step publishes a **config schema** in the catalog
(D3): each key's type, default, allowed values and description. The component validates every
step's `config` against it at render, so a typo or a wrong type fails the Application rather
than a run. Schemas only grow compatibly: adding a key is a compatible change; renaming,
removing or changing a key's meaning is a new step version (D9). §3.3 lists the starting keys —
for example the coder's `instructions` (a repo file of team conventions), holdout's
`testCommand`, security-scan's `ignore` list.

Where a parameter could weaken a gate, the platform bounds it in `factoryPolicy`, per
environment:

```yaml
factoryPolicy:
  stepConfig:
    platform/security-scan: {blockLevel: {atMost: high}, ignore: {requireReason: true}}
    platform/holdout:       {threshold: {atLeast: 0.8}}
```

Render enforces these bounds like every other policy rule (D7), naming the violated bound. A
parameter a team needs that a step does not have is a request for a new key — a compatible,
additive change to that step.

**2. Team logic between steps, after v1.** Teams will eventually want small pieces of their own
logic between platform steps — a script that reformats output, enforces a house rule, or
prepares data for the next step. That is the team-step version (§8 item 1), possibly with a
lighter "inline script" form that runs a team-supplied script in a platform-provided image. v1
does not build it, but every choice below keeps it a **two-way door**:

| What a later team step or script needs | What v1 already provides |
|---|---|
| A place in the process | Steps are generic DAG tasks addressed `<tier>/<name>` (D3, D5). A team step is a new tier, rendered like any other task; a stage can mix tiers |
| A way to run untrusted code | The runner already launches sandboxes from #151 templates with no credentials, for holdout (D4). A team step or script is one more template and image in the same path |
| Its source | The per-run workspace volume and clone pod (D4) are generic; any launch can mount them |
| Data from earlier steps, and a way to pass data on | `outputs` in the verdict and `DF_INPUTS` (§3.1) are part of the contract in v1, used by `changeset` today |
| A contract it can build against | The §3.1 contract is the same for every step, carries `DF_CONTRACT`, and is written as an ABI (D2) |
| A way to gate or not | Its verdict is posted by the platform under `dark-factory/team/<step>`; `onFail` works the same; it cannot satisfy `requiredSteps` (D7) |
| No change to the Application's shape | A team step is one more entry in `process.stages[].steps`, inside `properties`, so D20's admission allowlist is unchanged |

**What v1 must not do**, because each would close that door:

- **No hidden coupling between steps.** Platform steps must not share a pod, files outside the
  workspace, or in-memory state, and must not assume which step runs before or after them.
  Everything a step consumes arrives through `DF_*`, `DF_INPUTS`, GitHub or the workspace.
- **No step-specific rendering.** The renderer treats every step as a task with an image,
  config and contract; a step that needed special rendering would be one a team step could
  never sit next to.
- **No platform step that depends on running last in a stage**, or on the order of steps within
  a stage, since a team step may be inserted anywhere.
- **The `platform/`-only rule is a v1 render rule, not part of the schema.** Lifting it later is
  a policy change, not a migration.

**Cost:** each platform step owns a schema to maintain; `factoryPolicy` gains per-step bounds;
and the `outputs` channel must stay small and documented, since a team step will rely on it.

---

## 5. Security model

| Threat | Control |
|---|---|
| Coder code reaches credentials | VM-isolated sandbox; no Kubernetes token, no IAM; only a narrow, hour-long GitHub token as a 0400 file |
| Coder or holdout launch reaches the control plane | Label-keyed Admin network policy: private ranges denied, DNS only to the cluster resolver, public 443 only where policy allows, ingress only from the pod's own namespace (D13) |
| Another token posts a passing check with the same name | Merge and the ruleset accept only check-runs from the platform App; only the orchestrator token can create them (§3.1, D15) |
| A team runs its own code as a step | Not possible in v1: every `use:` must name a `platform/` step, checked at render (D3, D7) |
| Coder games its own tests | Hidden scenarios only holdout can reach; a judge from a different model family; fails if scenarios are missing (D17) |
| Holdout runs coder output on the hub | In sandboxed launches with no token and only this repo's scenarios; `npm install --ignore-scripts`; no AWS identity in the namespace (D4, D16, D17) |
| Holdout's scenarios leak | The test launch has no network, DNS included; it starts only after egress is refused (Kata) or with no network device (Nitro); its log and `summary` are never surfaced; it mounts the workspace read-only; each run gets a new volume that is deleted after use (D4, D13, D17) |
| Team creates arbitrary objects through its Application | Allowlist admission: only `type: dark-factory` components with `name` and `properties`; no `*Definition` kinds (D20) |
| Team pod mounts a factory token | The team can create only its Application, which can produce only the platform's pods; the definition exposes no pod fields or image choice; the coder image is set by onboarding (D7, D20) |
| A team's own sandbox falls under, or escapes, the factory's network rules | D13 selects only pods in platform-labelled namespaces with the platform's fixed component names; teams can set neither (§3.5, D20) |
| A factory gets tokens for repos it was not onboarded for | Tokens are minted from the onboarding record, which teams cannot write; the push Role can only update that factory's own Secrets (D15) |
| A factory syncs another factory's secrets | The secrets-store prefix is derived from namespace and name, with no override (§3.4) |
| Unreviewed or failing code is merged | Merge only after human approval; `platform/merge` refuses unless every required check is green at the PR head; a missing check is a refusal |
| Platform step code is tampered with | Each step's image is digest-pinned in a cluster-scoped, platform-owned template; only a reviewed platform commit changes it; each run records its digests (D9, D10) |
| A team's process drops a required gate | Three overlapping checks: render, merge and the ruleset (D7) |
| Runaway revision loop | One iteration budget shared by `revise` and comments; the factory's own comments are filtered at the Sensor (D8) |
| Two runs for one issue | Mutex and claim keyed on owner, repo and issue number; a concurrency semaphore (D8, D21) |
| Cross-factory interference | One factory per namespace; per-namespace events and secrets; namespace-scoped Argo locks (D12, D14, D15) |

**Residual risks**

- **The ruleset is the only hard stop on self-merge** (D20). Private-repo rulesets need a paid
  GitHub plan. With a repo-level ruleset, the team's repo admins can turn it off; the factory then
  refuses to merge, but a human could still merge in the UI. An organization ruleset closes this.
- **Render-time policy covers only the component path.** Team RBAC stops teams submitting raw
  Workflows (D20), but a cluster admin can bypass it. `templateReferencing: Strict` would close
  this cluster-wide (§8).
- **A valid process can still be a weak one.** A team can compose a process that passes policy but
  verifies little, or gate on a `ci-check` that always passes. Policy sets a floor for our gates,
  not the quality of the team's.
- **Holdout's install launch can leak source where `stepEgress: public`.** Coder-written install
  scripts can send the branch to any public host; v1 has no per-host limit (D13). That is why
  `stepEgress` is per-environment policy, defaults to `none`, and why an environment needing a
  host allowlist adds one at the VPC.
- **The App private key is the highest-value secret.** It can mint any of the App's permissions
  on every installed repo, across every factory. It lives only in a platform namespace, used only
  by External Secrets' generators (D15).

---

## 6. Operations

**Platform, once per hub**

1. Register the platform GitHub App and store its private key in the platform namespace (D15).
2. Set the sandbox runtime on the cluster, as for `type: sandbox`. Nitro needs nothing. A Kata
   cluster sets `sandbox_runtime_class: kata-clh` and `confidential_sandbox_runtime_class: none`
   through `<hub>/config` (#151 `registry/agentcore.yaml:82-84`); `sandbox_runtime: kata` alone
   moves only the agent-sandbox chart's pool.
3. Make the platform step images pullable in the hub's account (D10).
4. Set `enable_agent_sandbox` (through `sandbox.enabled`) for the operator and CRDs, and
   `enable_dark_factory_templates` for this chart — not `enable_dark_factory` (D21).
5. Set `factoryPolicy` for the environment (D7). The shipped default is production-shaped:
   `platform/security-scan` required, one approval, no registries allowed until named, and
   `stepEgress: none`.
6. Enable `enable_oam_components`, regenerate the component YAML with
   `platform/oam/generate.sh`, and **commit it**: `oam-agent-components/templates/dark-factory.yaml`
   is a generated, checked-in file, and the chart cannot register the type without it.

**Platform, per factory.** Add an `onboarding.factories` entry (D20).

**Team, per factory**

1. Apply the Application, starting from `preset: standard`.
2. Install the platform App, and the Security Agent and DevOps Agent Apps, on each repo (or leave
   those steps out where policy allows).
3. Have an organization owner apply the D20 ruleset (or apply it at repo level where that is all
   the plan allows). Status reports anything missing.
4. Add the holdout scenarios at the paths status reports as missing. Tokens are minted, not added
   by hand.
5. Connect each repo to an Agent Space for the DevOps Agent. This is console work
   ([`AGENT-INSTALL.md`](AGENT-INSTALL.md) A1–A3); the vendor offers no API for it.

**Team, for its own tooling.** Run it as a GitHub check where the team already runs CI, and add it
with `platform/ci-check` (D18).

**Changing the process.** Edit `process`; KubeVela re-renders it and the next run uses it.
In-flight runs finish on the old shape. A `factoryPolicy` violation fails at render.

**Upgrading platform steps.** A compatible fix keeps its version. An incompatible change ships as
a new version; factories move by bumping `catalogVersion` or that step's `version`, and the old
template stays until every factory has moved (D9). Changing the coder image is an edit to the
factory's onboarding entry.

**Deleting a factory.** Deleting the team's Application removes what the component created:

- **In-flight runs** are deleted with their Workflows; their claims, holdout sandboxes and volumes
  go with them, and `shutdownTime` reaps any sandbox left behind (§3.2).
- **Webhooks** are deregistered when the EventSource is deleted; status reports any that could
  not be.
- **Open `df/` PRs and branches stay open**, so work is not lost silently; their checks can no
  longer pass.

Offboarding (removing the onboarding entry) removes the namespace, the sandboxes Application and
the token objects. Tokens stop refreshing and expire within the hour. The team uninstalls the
platform App from repos no other factory uses.

---

## 7. Validation

**Already established** (on the existing hub, not this design's shape): `mutexes` accepted and
`mutex` rejected on Argo v4.1.4; a server-side dry run on `dora-showcase` of the component's
namespaced objects (the Sandbox and ExternalSecret CRDs are absent there); and the holdout
evaluator against sample scenarios with a judge that always says yes — an honest implementation
passed 4/4, a hard-coded one failed 2/4. To be re-run in the `platform/holdout` image (D10).

**Gates for the implementation, not this design.** The live end-to-end run and the
`configMapKeyRef` go/no-go are run by the implementer before the implementation merges, with
results recorded in that PR. If the go/no-go fails, the implementation uses D11's fallback. This
design doc does not wait on either.

**Go/no-go checks**

- **`configMapKeyRef` resolves in the run's namespace** for a run of the namespaced
  `dark-factory-process` whose tasks reference cluster-scoped templates. If not, the component
  renders settings into `dark-factory-process` as literal defaults (D11).
- **Workspace volumes on each runtime (D4).** Nitro mounting EBS is documented. Still to confirm:
  a Nitro launch mounting the volume **under `networkIsolated`**, and a Kata launch mounting it,
  each after the clone pod has released it, in the same zone. If a runtime fails, holdout on it
  uses the `exec` fallback.

**Tests**

- **Tokens (D15):** a namespace gets tokens only for repos in its onboarding record; a repo only in
  the Application gets none and shows in status; the push store can only update that factory's
  four Secrets and cannot read the key; tokens refresh every 20 minutes and stop after
  offboarding; a second repo owner fails render.
- **Selecting factory pods (§3.5):** EKS's `ClusterNetworkPolicy` accepts a namespace selector
  combined with a pod selector (if not, use the namespace-only fallback); the policy matches the
  coder, both holdout launches and the clone pod, and does not match a sandbox named
  `dark-factory-coder` in a namespace without the onboarding label; a team cannot set the
  onboarding label on any namespace.
- **Holdout launches (D4):** a launch `Sandbox` differs from its template only in the run-specific
  fields §3.2 lists.
- **Tenancy (D20):** in a factory namespace, admission rejects an Application with any `spec` field
  but `components` (including `workflow`, `workflow.ref`, `policies`, `sources`), any component
  field but `name`, `type` and `properties`, any `type` but `dark-factory` or one with `@version`,
  and any trait; any `*Definition` is rejected; no property reaches a pod's spec, volumes, env
  sources, labels or image; a non-`platform/` `use:` fails render; the sandboxes Application is
  admitted only from the platform's GitOps identity; a tag-pinned or off-registry coder image fails
  onboarding; the secrets-store prefix cannot be overridden.
- **Holdout leaks (D17):** the test launch's DNS queries are refused; its log and `summary` appear
  nowhere on GitHub or in revision notes; its workspace is read-only; a second run of the same issue
  gets a new volume.
- **Network (D13):** both Admin policies admitted with distinct priorities; **per runtime** (Kata on
  Karpenter, Nitro on Auto Mode), the policy covers holdout's launches and its CIDR rules are
  enforced for the guest — the gate for `stepEgress: public`. On Nitro, also whether `domainNames`
  rules work (§8). Which resolver #151's templates use, that exactly it is allowed on port 53, and
  that nothing else in the service range is reachable. On Kata, the init gate holds the test
  container until a public probe is refused; on Nitro, `networkIsolated` leaves no network device.
- **Results (§3.1, D4):** a `$DF_RESULT` written inside a Kata guest and inside a Nitro
  `networkIsolated` guest arrives as the termination message; a finished launch reaches
  `Succeeded`; a result over 4 KB, or malformed, is a `failure`.
- **Step contract:** a conformance suite with a reference step in two languages, plus adversarial
  cases — no result file, malformed JSON, non-zero exit, `success` after an internal error, and
  launch images reaching for the API server, a GitHub token and the network (these must fail
  closed). A `dark-factory/*` check not created by the platform App must be refused by merge. The
  suite later becomes the team-step conformance test (§8).
- **Step config (D22):** a `config` key a step's schema does not define, or of the wrong type,
  fails render naming the step and key; a value outside a `factoryPolicy.stepConfig` bound fails
  render naming the bound; a step's `outputs` reach a later stage's steps as `DF_INPUTS`; an
  oversized `outputs` makes the result unparseable and the step a `failure`.
- **Render:** CUE checks for each preset, `addSteps`, an explicit process, each `factoryPolicy`
  violation (must fail naming the rule), and a non-`platform/` `use:` or `requiredSteps` entry
  (must be rejected); a golden test per preset for stage order, steps and the checks merge
  requires; per-step tests against the contract, each starting with an empty `DF_PR_NUMBER`;
  `helm lint` and `argo lint` on every template; every `configMapKeyRef` key present in the
  rendered config.
- **Isolation:** `scripts/nitro/verify-isolation.sh` against a claimed coder and a holdout launch,
  showing each has its own guest kernel.
- **Timing:** sandbox cold-boot time per runtime for holdout's launches, to decide whether they need
  a warm pool.
- **End to end:** a live run on `preset: standard` (label → sandbox → PR → steps → approval →
  merge), then one with a `ci-check` failure and `onFail: revise`.

---

## 8. Follow-ups

1. **Team-written steps — the next version.** Lets a team run its own step image in the factory, as
   `use: <team>/<name>`. It adds: the §3.1 contract published as a versioned ABI; a step registry,
   and later a `shared/` tier; the runner launching team images in `type: sandbox` sandboxes;
   per-step capabilities (`workspace`, `egress`, `secrets` from a team-owned prefix); and
   `allowedRegistries` with digest pinning for team images; and possibly a lighter **inline
   script** form — a team script run in a platform-provided image between platform steps. D22
   lists what v1 already provides for it. It must keep three rules: a team step
   holds **no credentials** and cannot post a check; it has **no merge authority**, because
   `requiredSteps` stays limited to `platform/` steps; and it runs **isolated**, like holdout's
   launches (D4, D13). Starts once the platform steps run end to end.
2. **Shrink the reserved set** per D1, once team steps exist: `changeset` decided from a
   team-supplied path list, and holdout's evaluator as a team step given scenarios it cannot leak.
3. **Cosign verification of images**, alongside `allowedRegistries`.
4. **A warm pool for holdout's launches**, if boot time matters.
5. **Deliver run inputs without claim env**, so the coder warm pool is actually used and can
   default above 0 (§3.4).
6. **Move the hub factory onto the step templates**, removing the duplicated pipeline, and **port
   the chart's templates to `mutexes`** — they fail on Argo v4 today.
7. **Evaluate `templateReferencing: Strict`** on the hub's Argo controller — the only way to close
   the inline-Workflow bypass.
8. **Update `setup-secrets.sh`** for the App key, per-factory paths and holdout scenarios.
9. **Implement D10:** one image per platform step, honoring the contract, pinned by digest, with a
   build-and-publish pipeline.
10. **A status reconciler** instead of long `customStatus` expressions (D20).
11. **Per-host egress** for holdout's install launch: `domainNames` on Nitro once §7 confirms it, or
    a VPC firewall elsewhere (D13).
12. **Per-repo concurrency limits** alongside `maxConcurrentRuns` (D8).
13. **A second forge**, through the four seams in §2.

---

## Appendix: files

Planned layout. Of these, only the chart sources, `31-clusternetworkpolicy.yaml` and `PROFILES.md`
exist on `main` today.

| Path | Role |
|---|---|
| `platform/oam/definitions/components/dark-factory.cue` | The component: per-namespace resources, `process` schema, step resolution, presets, policy checks, the rendered process |
| `gitops/addons/charts/oam-agent-components/templates/dark-factory.yaml` | Generated by `platform/oam/generate.sh`. **Checked in**; the chart cannot register the type without it |
| `docs/dark-factory/STEP-CONTRACT.md` | The step contract (§3.1), internal in v1, published when team steps arrive (§8) |
| `gitops/addons/charts/dark-factory-templates/` | Platform step templates (each pinning a digest), the step runner, the catalog ConfigMap, the contract, `factoryPolicy` |
| `gitops/addons/charts/dark-factory-templates/` → `onboarding.factories` | One entry per factory. Renders the namespace, team Role, admission policy, quota, the sandboxes Application and the D15 External Secrets objects |
| `examples/dark-factory/platform-steps/` | One image per platform step, built from the chart's scripts (D10) |
| `platform/oam/examples/example-dark-factory.yaml` | Example: preset form, plus a `ci-check` on a team's own check |
| `gitops/addons/registry/sandbox.yaml` → `dark-factory-templates` | Registers the chart under its own `enable_dark_factory_templates` gate (D21) |
| `gitops/addons/charts/agent-sandbox/templates/31-clusternetworkpolicy.yaml` | The label-keyed Admin network policy (D13) |
| `gitops/addons/charts/dark-factory/scripts/`, `holdout/evaluate.js` | Source for the platform step images, until the hub factory moves onto them (§8 item 6) |
| `platform/oam/definitions/components/sandbox.cue` (#151) | Used unchanged (§3.5) |
| `docs/dark-factory/PROFILES.md` | Why there are no language profiles, and how presets differ (D6) |
