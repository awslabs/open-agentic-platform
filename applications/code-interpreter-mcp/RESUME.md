# Resume here: code-interpreter-mcp

Last updated 2026-08-12. Read `DESIGN.md` first for what this is and why. This file is only
state and remaining work. Issue #51 tracks the overall task.

## State

- **Branch:** `feature/agentcore-code-interpreter`, created off `feature/agentcore-browser-mcp`
  at commit `ae55c35`. It needs that branch's `mcp-server` work (selector-based gateway targets
  with session affinity, optional `replicas`), so do not rebase onto `main` until #49 merges.
- **No PR yet.**
- **Deployed and verified on `peeks-hub`.** ArgoCD tracks this branch (set via `config.local.yaml`).

### Done

| Thing | Where | Evidence |
|---|---|---|
| Server | `src/` | E2E PASS and TASK EVICTION PASS against the live service |
| Tests | `test/` | `e2e-multisession.js`, `task-eviction.js`, `concurrency-cost.js` |
| Dockerfile | `Dockerfile` | builds; 170MB; PID 1 is `node`, no tini, no children |
| Published image | `public.ecr.aws/z0a4o2j5/code-interpreter-mcp:0.1.1` | OCI index, `linux/amd64` `sha256:be1d5f2a697056b55f93bf1c6fff3bc32dee7b1897ab3ac8bdfd38089b41c2ac`, `linux/arm64` `sha256:bc7522a1fd94f801a8c9ade1546a0a77e424d68a3290ca3e4324cd5732888901` (0.1.1); pulled fresh and served `/readyz` with 9 tools |
| OAM component | `platform/oam/definitions/components/agentcore-code-interpreter.cue` | `generate.sh` produced only the intended file; rendered with `--set global.awsRegion=eu-west-1 --set global.awsAccountId=111122223333` and both flowed with no leftover placeholders; `kubectl apply --dry-run=server` accepted it |
| IAM policy | in that CUE | four granted actions `allowed`, three negative controls `implicitDeny` |
| Example app | `platform/oam/examples/example-code-interpreter-mcp.yaml` | resource sizing derived from measurement |
| Measurements | `DESIGN.md` | per-session memory below noise floor; 8 simultaneous activations at 1.3 to 1.4s, zero failures |
| On-cluster deploy | `peeks-hub` | interpreter provisioned, `dependsOn` gated, 13 IAM AccessDenied cycles ridden out with 0 restarts |
| Gateway path | `test/gateway.js` | GATEWAY PASS with a real JWT |
| All 9 tools | `test/tool-matrix.js` | TOOL MATRIX PASS; found and fixed the `executeCode` language defect (0.1.1) |
| Real agent | `code-agent` app | returned a sha256 digest matching a locally computed one |

The ECR Public repository `code-interpreter-mcp` did not exist and was created as part of
publishing, matching the existing `browser-mcp` repository.

### How to run what exists

```bash
cd applications/code-interpreter-mcp
npm install
eval "$(aws configure export-credentials --format env)"

# against the AWS built-in interpreter, no provisioning needed
AWS_REGION=us-west-2 MCP_PORT=8031 node src/server.js
BASE_URL=http://localhost:8031 node test/e2e-multisession.js         # expect E2E PASS

# task eviction needs a short idle window, so run a second server
AWS_REGION=us-west-2 MCP_PORT=8032 SESSION_IDLE_SECONDS=15 node src/server.js
BASE_URL=http://localhost:8032 IDLE=15 node test/task-eviction.js    # expect TASK EVICTION PASS

# measurement, in the container
BASE_URL=http://localhost:8033 N=8 PARALLEL=1 HOLD_SECONDS=60 node test/concurrency-cost.js
```

## Remaining work

1. **PR**, referencing #51. Base it on `feature/agentcore-browser-mcp`, or `main` once #49
   merges. Everything in DESIGN.md is verified, including on-cluster, so no hedging is needed.
2. Optional cleanup: the example apps `code-tools` and `code-agent` are still deployed on
   `peeks-hub`, and `config.local.yaml` still points ArgoCD at this branch. Both were
   deliberate for verification.

Everything else is done. On-cluster verification is recorded in DESIGN.md: provisioning,
`dependsOn` gating, the retry loop riding out 65s of IAM propagation with 0 restarts, the
enforced IAM policy read back from AWS, gateway registration, E2E PASS, GATEWAY PASS, TOOL
MATRIX PASS, and a real agent executing code whose output could not be guessed.

### Follow-ups worth filing separately

- The AWS SDK warns that versions published after early January 2027 will require node >= 22,
  and both this image and browser-mcp are on `node:20-alpine`. A base image bump is a shared
  change, not specific to this server.
- `test/tool-matrix.js` should ideally run in CI, since it is the guard against advertising a
  tool that fails when called the way its schema permits.

## Correction to an earlier note in this file

An earlier version of this document said the `crossplane-agentcore` chart's provisioning
"already works, do not rebuild it." **That was wrong at the Crossplane layer.** The
interpreters do exist in AWS and report `READY`, which is what the spike observed through the
AWS API, but the chart's managed resource has been failing since 2026-05-27 with
`ConflictException: CodeInterpreter with name 'peeks_hub_agent_core_code_interpreter' already
exists in this account`, and its `atProvider` is empty. The chart's browser resource is stuck
the same way. See the Provisioning section of `DESIGN.md`.

The part that stands: do not rebuild the provisioning layer for this component, because
`agentcore-code-interpreter` provisions its own interpreter exactly the way
`agentcore-browser` does, and that path demonstrably works (`web-browser` has
`browserId=agents_web_browser-gexwcAoVXk`). The lesson is about the failure mode, not about
the approach.

## Open questions

- `sessionTimeoutSeconds` maximum. 900 is the default because that is what the browser uses;
  the code interpreter's ceiling is unconfirmed.
- Account quota on concurrent code interpreter sessions. This now matters more than pod memory,
  since memory does not bound session count.
- `sessionId` is typed optional on `InvokeCodeInterpreter`. If omitting it creates an implicit
  session, that is a leak nothing would surface. The server always passes one, so this is about
  documenting the hazard.
- `filesystemConfigurations` and `certificates` on session start, unprobed.
- Whether `executeCode` with `language: javascript` or `typescript` needs `runtime` set. Only
  python was exercised.
- Whether `networkMode` accepts values beyond `PUBLIC` and `VPC`. The CRD does not enumerate
  them, so the parameter is left open rather than guessed; only `PUBLIC` is verified.

## Gotchas already paid for

- `generate.sh` needs a reachable KubeVela cluster: `KUBECONFIG=.platform/private/hub-kubeconfig`.
- Regenerating no longer damages `agent.yaml`; the `opentelemetry-instrument` command was moved
  into `agent.cue` on the browser branch. Confirmed: a full regeneration touched nothing else.
- `kubectl get application` resolves to ArgoCD's CRD. Use `applications.core.oam.dev` for OAM.
- The workload is named after the **component**, not the Application.
- Crossplane `CodeInterpreter` and IAM `Policy` are cluster-scoped.
- `podman build` on this machine defaults to amd64 even on an arm64 host, which silently makes
  local measurements run under emulation. Pass `--platform linux/arm64` for local testing.
- `skopeo inspect` without `--raw` fails on a multi-arch OCI index. Use `--raw` and read the
  `manifests` array.
- ECR Public requires the repository to exist before a push; the failure is
  `name unknown: The repository with name '<x>' does not exist`.
- `podman stats` memory is not reliable for small deltas. It reported less memory with eight
  sessions than at idle. Read RSS from inside the container instead.
- Never attach the shell to a long-running server. Run it detached and poll with bounds. macOS
  has no `timeout`; use `perl -e 'alarm N; exec @ARGV' <cmd>`.
- Do not type versions, digests or ids from memory. Paste them from tool output.
