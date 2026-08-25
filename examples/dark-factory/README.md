# Dark Factory — Flow B coder image

The **in-VM coder** for the Dark Factory (Flow B). This directory builds the container image that
runs **inside the Kata micro-VM sandbox** — the untrusted side of the trust boundary. Orchestration
(trigger → claim → drive → PR → teardown) is **not** here: it's done declaratively by **Argo
Workflows on the hub** (see the [`dark-factory` Helm chart](../../gitops/addons/charts/dark-factory/)
and [`docs/dark-factory/README.md`](../../docs/dark-factory/README.md) §4).

> **History:** an earlier P1 used a bespoke long-running **Node orchestrator** (`orchestrator/`) and
> an HTTP-server coder (`coder/agent.js`). Both were removed once Flow B moved to Argo Workflows —
> the orchestrator's responsibilities are now the `df-run` WorkflowTemplate's DAG steps (a `resource`
> template creates the `SandboxClaim`; a `script` step polls GitHub; `onExit` releases the claim),
> and the coder became **credential-less + self-reporting** (`entrypoint.js`).

## What's here

| Path | Role | Trust |
|---|---|---|
| `coder/entrypoint.js` | The in-VM coder. Auto-runs on VM start; reads the `DF_*` env the `SandboxClaim` injects, fetches the issue as `SPEC.md`, checks out `df/issue-<n>`, runs Claude Code headless via Bifrost, builds + tests, pushes the branch, opens the PR, and sets the `dark-factory/implementation` commit status. | **untrusted** (Kata VM, no cloud creds, no k8s API) |
| `coder/Dockerfile` | Lean `node:20-alpine` + git/bash/python3/go + the Claude Code CLI. Carries **no** credentials. | — |
| `deploy-test/Dockerfile` | kubectl + terraform + node/git/curl for the hub-side `deploy-test` step. The **only** step holding K8s access. | trusted (hub) |
| `setup-secrets.sh` | Writes the three GitHub credentials to Secrets Manager. Run once before the first factory run — see [Credentials](#credentials) below. | trusted (hub) |

## How the coder is driven (single-cluster on the hub)

```
GitHub issue (label: dark-factory)
  → Argo Events: GitHub webhook EventSource → Sensor
  → df-run WorkflowTemplate (argo ns, per-issue mutex)
       ├─ claim  : resource template creates SandboxClaim(warmPoolRef=coder-warmpool)
       │            with the issue injected as env (DF_ISSUE_NUMBER/REPO/BRANCH/…)
       │            → operator binds a warm Kata VM (Flow A), status Ready
       │   ┌── coder VM (this image) auto-runs entrypoint.js on start:
       │   │     issue → SPEC.md → checkout df/issue-N → claude implements →
       │   │     build + unit tests → push branch → open PR →
       │   │     POST commit status dark-factory/implementation = success|failure
       │   └── credential-less to the k8s API → self-reports through GitHub
       ├─ drive-coder : script step POLLS the GitHub API for a PR on df/issue-N and
       │                reads the head commit's dark-factory/implementation status
       ├─ status : sticky status (P1 stops here — PR open, awaiting human)
       └─ onExit : teardown — delete the SandboxClaim (operator refills the pool)
  → human reviews the PR, approves, merges
```

## The `DF_*` contract (env the claim injects)

`entrypoint.js` is entirely env-driven. The `df-run` claim step sets these via `SandboxClaim.spec.env`
(verified contract: the SandboxTemplate opts in with `envVarsInjectionPolicy: Overrides`):

| Var | Purpose |
|---|---|
| `DF_REPO` | `owner/name` of the target repo |
| `DF_ISSUE_NUMBER` | the GitHub issue number (the spec) |
| `DF_BRANCH` | `df/issue-<n>` |
| `DF_BASE_BRANCH` | base to branch from (default `main`) |
| `DF_ISSUE_TITLE` | issue title (for the PR title) |
| `CODER_PROFILE` | `claude-code` (primary) or `kiro` |
| `BIFROST_URL` | LLM gateway — the **ClusterIP** (the Kata VM guest DNS can't resolve svc names) |

Secrets are **not** in env: the short-TTL GitHub token (+ optional Bifrost key) are projected into
the VM at `/etc/secrets` (tmpfs, mode 0400) and read at point of use.

## Bifrost gotchas baked into `entrypoint.js`

The Claude Code CLI talks to models only through the platform's **Bifrost** gateway. Four issues had
to be solved to make `claude -p` work inside the locked-down VM (see the commit history + memory):

1. **User-Agent routing** — Bifrost 400s (`Unexpected field type`) on any request whose UA starts
   with `claude-cli`. `entrypoint.js` fronts Bifrost with a **localhost UA-shim** (a separate `node`
   process on `127.0.0.1:8791`) that rewrites the UA and transparently forwards everything (incl. the
   SSE stream). `ANTHROPIC_BASE_URL` points at the shim.
2. **Writable HOME** — `readOnlyRootFilesystem` makes `$HOME` read-only, so Claude Code can't create
   `~/.claude` (shell snapshots its Bash tool needs). `HOME`/`CLAUDE_CONFIG_DIR`/XDG are pointed at
   the writable `/tmp` tmpfs.
3. **Model alias** — use `ANTHROPIC_MODEL=claude-sonnet` (a Bifrost alias → `us.anthropic.claude-sonnet-4-5`).
   Do **not** set `CLAUDE_CODE_USE_BEDROCK` (that bypasses the base URL and needs in-VM AWS creds).
4. **`git push --force`** (not `--force-with-lease`) — the depth-1 clone can't satisfy the lease
   check; `df/issue-N` is bot-owned + single-writer (per-issue workflow mutex), so `--force` is safe.

The GitHub self-report helper retries transient failures (transport/5xx/429) so a blip on the report
call can't mark a good run failed.

## Trust boundary (§10)

- The **coder is untrusted**: Kata micro-VM (own kernel), `automountServiceAccountToken: false` (no
  k8s API), no cloud IAM. Its only credentials are a Bifrost key + short-TTL GitHub token via
  projected tmpfs (0400). Flow A's NetworkPolicy + node ClusterIP firewall lock egress to Bifrost +
  DNS + GitHub. Because it holds no k8s creds, it **self-reports through GitHub** (the workflow polls).
- The **AWS IAM stays with the hub orchestrator** (Argo), never in the VM.
- This breaks the **lethal trifecta** (untrusted issue text + credentials + egress): credentials are
  never in the issue-ingesting sandbox context, and egress is denied by default.

## Build & deploy

The image is built + pushed to ECR and pinned on the Flow A `SandboxTemplate` via GitOps. **No
per-cluster overlay is needed:** the addon registry
([`gitops/addons/registry/sandbox.yaml`](../../gitops/addons/registry/sandbox.yaml)) builds each image
URI from the target cluster's own `aws_account_id` + `aws_region` annotations, so the same commit
resolves to whatever account it is deployed into and no account ID is committed to this repo.

```bash
# amd64 (hub nodes are amd64); podman/docker both work.
aws ecr create-repository --repository-name dark-factory-coder --region <region>   # first time only
podman build --platform linux/amd64 -t <acct>.dkr.ecr.<region>.amazonaws.com/dark-factory-coder:<tag> \
  examples/dark-factory/coder
podman push <acct>.dkr.ecr.<region>.amazonaws.com/dark-factory-coder:<tag>
# → bump the tag in the registry entry (valuesObject), commit, let ArgoCD sync.
```

Templating the registry **does not build the image** — each deployment must build it into its own ECR
at the pinned tag, or the warm pods report `ImagePullBackOff`. If your ECR repo uses **immutable
tags**, bump the tag rather than re-pushing one.

Three values in the registry reuse this same coder image (`coderTemplate.image` on `agent-sandbox`;
`reviewImage` and `holdout.evalImage` on `dark-factory`) — repoint them together. `deployTest.image`
is a **separate** build (`deploy-test/`): it needs kubectl + terraform, which the coder image does not
carry, and it is wrapped in a Helm `required` guard, so the app fails to render if it is unset.

## Credentials

The factory needs **three separate GitHub credentials** so the untrusted coder VM never holds a
token that can administer webhooks. They come from AWS Secrets Manager via external-secrets; nothing
is created by hand in-cluster.

```bash
export EVENTS_TOKEN='github_pat_...' ORCHESTRATOR_TOKEN='github_pat_...' CODER_TOKEN='github_pat_...'
bash examples/dark-factory/setup-secrets.sh
```

Minting the PATs is **manual** — GitHub ships no PAT-creation API, so this cannot be scripted. The
exact per-token permission sets are in the script's header comment, and the rationale (plus which
API call each permission is needed for) is in
[`docs/dark-factory/README.md` §10a](../../docs/dark-factory/README.md#10a-github-credentials-secrets-manager-setup).

Symptom when they are missing: the warm pods sit in `ContainerCreating` indefinitely with
`MountVolume.SetUp failed … secret "dark-factory-github-coder" not found`. Nothing crashes and
nothing retries visibly — the mount just never satisfies.

## Roadmap

- **P1 (done):** issue → PR, end-to-end, hands-off. The workflow stops at "PR open, awaiting human."
- **P2:** holdout gate — an isolated eval Job runs hidden BDD scenarios the coder never sees.
- **P3:** AWS Security / DevOps review agents (advisory → blocking-capable).
- **P4:** auto-merge/teardown on approval + iterate loop (PR-comment driven).
