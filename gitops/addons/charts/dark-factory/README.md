# dark-factory — Flow B, Phase P1

**Argo Workflows on the hub that turn a GitHub issue into a PR**, claiming the
[Flow A](../agent-sandbox) warm Kata pool. This is P1 of the
[Dark Factory pattern](../../../../docs/dark-factory/README.md): trigger → claim
warm sandbox → coder implements + tests → open PR with live status → **stop,
awaiting human**. Verification gates (holdout, Security/DevOps), the iterate loop,
and merge/teardown are P2–P4.

## What it installs (hub only, `alwaysSelector: environment In [control-plane]`)

| Template | Resource | Role |
|---|---|---|
| `10-rbac.yaml` | SA `dark-factory-workflow` (argo ns) + Roles/Bindings | Workflow SA: executor perms in `argo`; sandboxclaims CRUD + read sandboxes/pods in `agent-sandbox-system`. No secrets, no exec, no cluster scope. |
| `20-workflowtemplate-df-run.yaml` | `WorkflowTemplate df-run` | The P1 pipeline: claim → await coder (GitHub poll) → status; `onExit` releases the claim. Per-issue mutex. |
| `30-workfloweventbinding.yaml` | `WorkflowEventBinding dark-factory` | Submits `df-run` from a POST to the argo-server events endpoint (the thin trigger). |

## How it works

1. **Trigger** — an issue labelled `dark-factory` fires the GitHub Action
   (`.github/workflows/dark-factory.yml`), which POSTs the issue to
   `argo-server/api/v1/events/argo/dark-factory`. The `WorkflowEventBinding`
   submits `df-run` keyed on the issue id.
2. **Claim** — the `claim` step creates a `SandboxClaim(warmPoolRef)` with the
   issue injected as env (`DF_ISSUE_NUMBER`, `DF_REPO`, `DF_BRANCH`, …). The
   operator provisions a **fresh** warm micro-VM with that env present at start.
3. **Code** — the coder image (baked into the Flow A SandboxTemplate) reads the
   `DF_*` env, fetches the issue as `SPEC.md`, implements on `df/issue-N` via
   Claude Code + Bifrost, builds + tests, pushes, and **opens the PR itself**,
   then sets the `dark-factory/implementation` commit status. The coder is
   credential-less to the k8s API, so **GitHub is the completion bus**.
4. **Await** — the `await-coder` step polls the GitHub API for the PR + that
   commit status (success/failure).
5. **Teardown** — `onExit` deletes the SandboxClaim (success *or* failure); the
   operator refills the pool. A reaper is the crash-net for force-killed runs.

Verified live end-to-end on the hub: claim binds, all `DF_*` env inject, the
coder container starts with the issue, and `onExit` releases the claim + refills
the pool (3/3).

## Activation (remaining, to run a real issue)

P1 is deployed and its **orchestration is proven** with the placeholder coder
image. To process a real issue:

1. **Build + push the coder image** (`examples/dark-factory/coder`, `entrypoint.js`)
   to ECR, then set `agent-sandbox` chart `coderTemplate.image` to it. Until then
   the warm pods idle on the busybox placeholder (claim mechanics still work).
2. **Provide the GitHub credentials** — three separate, differently-scoped
   credentials, provisioned from AWS Secrets Manager by ExternalSecrets (there is
   no longer a single shared `dark-factory-github` secret). See
   [docs/dark-factory §10a](../../../../docs/dark-factory/README.md#10a-github-credentials-secrets-manager-setup)
   for the SM keys, the exact GitHub permissions, and the `aws secretsmanager`
   commands.
3. **Wire the trigger** — set repo/org var `DARK_FACTORY_ARGO_SERVER` and secret
   `DARK_FACTORY_ARGO_TOKEN`, then label an issue `dark-factory`.

## Key values

| Value | Default | Purpose |
|---|---|---|
| `warmPool.name` | `coder-warmpool` | Flow A pool to claim from |
| `coder.profile` | `claude-code` | `claude-code` \| `kiro` |
| `coder.runTimeoutMinutes` | `30` | `await-coder` deadline |
| `maxConcurrentRuns` | `3` | cap in-flight runs vs kata pool |
| `claimTtlSeconds` | `10800` | reaper backstop on the claim |
| `github.tokenSecret` | `dark-factory-github-orchestrator` | PR + status + merge (hub, `argo` ns) |
| `trigger.argoEvents.githubSecret` | `dark-factory-github-events` | webhook HMAC + repo read (hub, `argo-events` ns) |
