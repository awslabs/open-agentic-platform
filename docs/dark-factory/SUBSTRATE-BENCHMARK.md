# Dark Factory — Substrate Benchmark: Kata micro-VM vs Lambda MicroVM

A side-by-side comparison of the two sandbox substrates that run the autonomous coder,
measured on **identical issues fired in parallel** on the same hub cluster.

- **Flow B — Kata micro-VM** (mature, default): the coder runs in a hardware-isolated Kata
  pod on a self-managed nested-virt EKS node group.
- **Flow D — AWS Lambda MicroVM** (pre-GA): the coder runs in a Firecracker MicroVM
  provisioned via the `lambdamicrovms` ACK controller, driven by a bridge pod.

Both run the **same `dark-factory-coder`** (same `entrypoint.js`), produce the same kind of
PR, and go through the **same review gates** (AWS DevOps Agent + AWS Security Agent). The only
difference is *where the coder executes* and *how it's provisioned*.

---

## TL;DR

| | Kata micro-VM (Flow B) | Lambda MicroVM (Flow D) |
| --- | --- | --- |
| **Workflow** | `df-run` (certified) | `df-run-lambda` (separate, MicroVM-native) |
| **Provisioning** | pre-warmed pool → **instant claim** | **RunMicrovm cold-start per session** (~90s) |
| **Time to first PR** (from label) | ~**2 min** | ~**2.5 min** |
| **LLM path** | Bifrost gateway (in-cluster) + Langfuse traces | **Bedrock-direct** (exec role) — no cluster network |
| **Scale-to-zero when idle** | ❌ node pool runs continuously | ✅ **suspend-to-zero** between PR and merge |
| **Fix-round mechanic** | fresh pod each round | **resume the SAME suspended VM** (warm); recreate if the pre-GA resume fails |
| **Infra to manage** | nested-virt node group (Karpenter/MNG) | none — serverless MicroVMs |
| **Observability** | native `kubectl logs` | custom `/logs` HTTP endpoint (no runtime CloudWatch) |
| **Maturity** | production-ready today | pre-GA (preview) — pilot-grade |
| **Economics at 1000s scale** | pay for idle capacity | pay per active minute (the strategic win) |

**Bottom line:** at small scale the two feel equivalent (the LLM coding step ~2–4 min and the
external review agents ~8–15 min dominate total time on *both*). The Lambda substrate's advantage
is **not latency** — it's **operational + economic**: no node pool to run, and suspend-to-zero
between the PR and the human's review/merge. Its cost is **maturity** (pre-GA control plane — resume
from suspend is occasionally flaky, mitigated by the recreate-fallback) and the extra plumbing below.

---

## Benchmarked run (identical issue, per substrate)

### Time to first PR (from label → PR opened)
| Substrate | Issue | PR | Elapsed | Notes |
| --- | --- | --- | --- | --- |
| Kata | #137 | — | ~**2 min** | pre-warmed pod, instant claim |
| Lambda | #135 | #136 | ~**2.5 min** (20:53:03 → 20:55:35) | native `df-run-lambda`, RunMicrovm cold-start |

**Δ ≈ 30–90s** — the MicroVM cold-start (`RunMicrovm` → RUNNING → `/run`) vs Kata's pre-warmed pod
claim. Note the MicroVM-native `df-run-lambda` is **faster than the old bridge path** (~3.7 min):
removing the SandboxClaim/warm-pool indirection cut ~1 min. Everything after (clone → LLM → push) is
identical code and takes the same time.

### Lifecycle timing (Lambda #135, native pipeline)
| Phase | Time |
| --- | --- |
| provision-microvm (RunMicrovm → RUNNING → `/run` HTTP 200) | ~90s |
| **drive-coder** (clone → LLM → push → PR) | ~60s |
| suspend-microvm (VM → SUSPENDED, stays down) | ~5s |
| holdout / deploy-test (terraform validate) | ~20–40s each |
| security-agent + devops-gate (external) | ~8–15 min combined (dominates) |
| fix round: resume-or-recreate + coder re-run → new commit | ~2 min |

*(The external review agents dominate total wall-clock on BOTH substrates. The MicroVM is SUSPENDED
for the entire multi-minute review window — that idle time is free on Lambda, billed on Kata.)*

---

## Where the logs are

| What | Kata (Flow B) | Lambda (Flow D) |
| --- | --- | --- |
| Pipeline steps | Argo UI (`/argo-workflows`) or `kubectl logs -n argo <pod>` | **same** |
| Coder output | `kubectl logs -n agent-sandbox-system df-issue-<id>` (native) | **`GET https://<vm-endpoint>/logs`** with an auth token (runtime CloudWatch routing is unreliable on the pre-GA runtime, so the hook-server captures coder stdout to a file + serves it) |
| Image build | n/a (normal ECR image) | CloudWatch `/aws/lambda/microvms/coder-image` |

---

## DAG — two SEPARATE WorkflowTemplates (one per substrate)

The substrates run **different Argo WorkflowTemplates**, so each graph is clean and Kata's certified
pipeline is never touched by Flow D changes. The Argo Events sensor routes by label:
`dark-factory` → `df-run` (Kata), `darkfactory-lambda` → `df-run-lambda` (Flow D).

**Kata — `df-run` (certified, byte-identical to the mature pipeline):**
```
claim(SandboxClaim) → drive-coder → { holdout, devops-gate → security, detect → deploy-test } → status → onExit(teardown: delete claim)
```

**Lambda — `df-run-lambda` (MicroVM-native; NO SandboxClaim / bridge / warm pool):**
```
provision-microvm → drive-coder → suspend-microvm → { holdout, devops-gate, security, detect → deploy-test } → status → onExit(keep suspended VM)
```
The one extra node — `suspend-microvm` — is **explicit and lives only in the Lambda graph**, so the
Kata graph still contains zero MicroVM nodes. Suspend/resume is owned by the workflow directly (it
calls `aws lambda-microvms suspend/resume-microvm`), not a bridge or a lifecycle controller.

### Substrate-specific mechanics
- **Kata:** `claim-sandbox` binds a **pre-warmed** pod from `coder-warmpool`; the operator injects
  `DF_*` env; the baked `entrypoint.js` runs in-cluster, reaches models via **Bifrost**, native logs.
- **Lambda:** `provision-microvm` (a single workflow step, running as `dark-factory-workflow` with the
  lambda-microvms role via Pod Identity) does it all — no bridge pod, no warm pool:
  1. reads the platform image handoff (imageARN + execRoleARN, built **once** by KRO/ACK),
  2. creates the **`Microvm` CR** (stable name `mvm-<issue-number>`) + a runHookPayload Secret
     (the review note is folded in here — the coder has no claim env),
  3. waits RUNNING + endpoint, mints an auth token, **POSTs `/run`** → the hook-server
     background-spawns the same `entrypoint.js` with `USE_BEDROCK=1` (Bedrock-direct, no cluster net),
  4. the `suspend-microvm` DAG step suspends the VM once the PR is open (idlePolicy
     `autoResumeEnabled=false` + nothing polls the endpoint → it **stays** suspended),
  5. on a fix round `provision-microvm` **resumes the same suspended VM** (warm resume); if the
     pre-GA service failed the resume (VM terminated), it **recreates a fresh VM** automatically,
  6. `df-merge-teardown` deletes the `Microvm` CR at merge → controller `TerminateMicrovm`.

---

## Step-by-step: what actually happens

### Kata (Flow B) — `df-run`
1. Issue labeled `dark-factory` → sensor dep `issue-labeled-kata` → `df-run`.
2. `claim-sandbox` binds a **pre-warmed** Kata pod from `coder-warmpool` (instant).
3. Operator injects `DF_*` → baked `entrypoint.js`: clone → Claude Code (via **Bifrost**) → **open PR**.
4. Review gates: DevOps Agent + Security Agent → consolidated verdict.
5. "fix findings" → `df-iterate` → **new** Kata coder round → re-review.
6. Approve → `df-merge-teardown` merges + deletes the claim.

### Lambda MicroVM (Flow D) — `df-run-lambda`
1. Issue labeled `darkfactory-lambda` → sensor dep `issue-labeled-lambda` → `df-run-lambda`.
2. `provision-microvm` creates the `Microvm` CR → controller `RunMicrovm` (**cold-start ~90s**) →
   RUNNING; mints token; `POST /run` → hook-server spawns the coder (`USE_BEDROCK=1`,
   **Bedrock-direct**). Coder: clone → Claude Code → **open PR**.
3. `suspend-microvm` step suspends the VM → it **stays SUSPENDED** while gates run (scale-to-zero).
4. Same review gates + verdict.
5. "fix findings" → `df-iterate` → `df-run-lambda` fix round: **resume the SAME VM** (warm) or, if the
   pre-GA service failed the resume, **recreate fresh**; the coder re-runs with the note → new commit.
6. Approve → `df-merge-teardown` merges + deletes the `Microvm` CR → controller `TerminateMicrovm`.

---

## Gotchas the Lambda substrate needed (that Kata does not)

Because a MicroVM is **outside the cluster network, has a read-only rootfs, and uses a
snapshot/hook execution model**:

| # | Gotcha | Fix |
| --- | --- | --- |
| 1 | Coder crashed `EACCES mkdir /workspace/artifacts` (no writable volume like Kata) | set `WORKSPACE=/tmp/workspace` (writable tmpfs) — *the silent killer* |
| 2 | Can't reach Bifrost's ClusterIP from a MicroVM | **Bedrock-direct** via the exec role (`bedrock:InvokeModel`); no Bifrost/NLB/VPC-connector |
| 3 | Runtime logs don't reach CloudWatch | hook-server captures coder stdout → `/logs` HTTP endpoint |
| 4 | Coder is one-shot but the MicroVM `/run` hook has a 30s timeout | `/run` **background-spawns** the coder + returns fast; pipeline polls GitHub for the PR |
| 5 | Ingress: `ALL_INGRESS` blocks auth-token minting | use **`HTTP_INGRESS`** |
| 6 | aws-cli image lacks `lambda-microvms`; no node | step image = `aws-cli:latest` (has the verbs) + python3 for JSON + fetch kubectl at start |
| 7 | `runHookPayload` is a `SecretKeyReference`; imperative `run-microvm --run-hook-payload` doesn't fire `/run` | deliver via the **declarative `Microvm` CR** |
| 8 | Image rebuild: overwriting the same S3 key doesn't rebuild | use versioned artifact keys; bump `codeArtifactKey` (the bucket half is composed by the RGD from the bucket it creates, so only the key is settable) |
| 9 | **VM would not stay SUSPENDED** (console showed RUNNING) | `idlePolicy.autoResumeEnabled=false` **and** never hit the VM endpoint after `/run` — any request auto-resumes it |
| 10 | **VM auto-terminated before the fix round** (resume hit "already terminated") | `suspendedDurationSeconds=28800` (Lambda's 8h max) so it survives the review→human window; 5 min was far too short |
| 11 | **Fix round re-ran but committed nothing** (coder "done" on old sha) | the review note must ride on the **runHookPayload** (MicroVM has no claim env); hook-server keys its `/run` guard on a **per-invocation run-id** so a resumed VM accepts a fresh run |
| 12 | Pre-GA **resume-from-suspend is intermittently flaky** (Internal service error → VM terminates) | `provision-microvm` checks the VM's real AWS state and **recreates a fresh VM** when resume isn't possible — the fix round self-heals |
| 13 | Two Sandboxes fought over one VM → suspend/resume **flapped** until the VM died | name the CR/workflow by **issue-number** (stable across rounds) so there's exactly **one VM per issue** |
| — | IAM: the workflow SA calls the lambda-microvms verbs | Pod Identity binds `dark-factory-workflow` → the lambda-microvms role (get/suspend/resume/terminate-microvm + create-auth-token); exec role keeps `bedrock:InvokeModel` |

> **Note on the ACK CR status:** `Microvm.status.state` is **stale** — it does not reflect
> suspend/resume/terminate. Always read AWS truth with `aws lambda-microvms get-microvm --query state`.

Kata needs **none** of these — it's an in-cluster pod with a mounted workspace, native logs,
Bifrost reachability, and a normal ECR image. That's why the two substrates are **separate
WorkflowTemplates** (`df-run` vs `df-run-lambda`): Flow D's plumbing never touches the certified Kata graph.

---

## When to choose which

- **Kata (today):** production-ready, mature, standard `kubectl`/IDE access, in-cluster networking.
  Choose it now for reliability. Cost: you run + pay for a nested-virt node pool continuously.
- **Lambda MicroVM (strategic):** serverless, suspend-to-zero per idle session, no node pool —
  the model that scales economically to thousands of sessions. Choose it as it reaches GA. Cost
  today: pre-GA control-plane maturity + the plumbing above.

Both share the **same coder, same pipeline, same review gates, same UX** — so migrating between
substrates is a label change, invisible to the developer/issue author.
