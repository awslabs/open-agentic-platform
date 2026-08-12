# Resume here: code-interpreter-mcp

Last updated 2026-08-12. Read `DESIGN.md` first for what this is and why. This file is only
state and remaining work. Issue #51 tracks the overall task.

## State

- **Branch:** `feature/agentcore-code-interpreter`, created off `feature/agentcore-browser-mcp`
  at commit `ae55c35`. It needs that branch's `mcp-server` work (selector-based gateway targets
  with session affinity, optional `replicas`), so do not rebase onto `main` until #49 merges.
- **No PR yet.**
- **Not deployed.** Everything below is verified locally or by dry-run, not on-cluster.

### Done

| Thing | Where | Evidence |
|---|---|---|
| Server | `src/` | E2E PASS and TASK EVICTION PASS against the live service |
| Tests | `test/` | `e2e-multisession.js`, `task-eviction.js`, `concurrency-cost.js` |
| Dockerfile | `Dockerfile` | builds; 170MB; PID 1 is `node`, no tini, no children |
| Published image | `public.ecr.aws/z0a4o2j5/code-interpreter-mcp:0.1.0` | OCI index, `linux/amd64` `sha256:3cc38c69ade2b46c402f2d43616297156fb11abbf1e41eabd6d45f0cab7c8e29`, `linux/arm64` `sha256:4bfd16cb2762c3bb764c1aedb34f0d43dde865cd49f748b5a4848f03d5da1cd2`; pulled fresh and served `/readyz` with 9 tools |
| OAM component | `platform/oam/definitions/components/agentcore-code-interpreter.cue` | `generate.sh` produced only the intended file; rendered with `--set global.awsRegion=eu-west-1 --set global.awsAccountId=111122223333` and both flowed with no leftover placeholders; `kubectl apply --dry-run=server` accepted it |
| IAM policy | in that CUE | four granted actions `allowed`, three negative controls `implicitDeny` |
| Example app | `platform/oam/examples/example-code-interpreter-mcp.yaml` | resource sizing derived from measurement |
| Measurements | `DESIGN.md` | per-session memory below noise floor; 8 simultaneous activations at 1.3–1.4s, zero failures |

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

## Remaining work, in order

### 1. Agent example

An agent app consuming this through the gateway, modelled on
`platform/oam/examples/example-agent-with-browser.yaml`: `mcpServers: [{name: code-interpreter-mcp}]`
plus the `gateway-identity` trait, and nothing else. No image change is needed on the agent
side.

### 2. On-cluster verification

ArgoCD currently reconciles `feature/agentcore-browser-mcp`, so **this branch does not deploy
on its own**. `config.local.yaml` (gitignored) is set to `main` but was deliberately not
re-bootstrapped, because `main` lacks #49's definitions. Either merge #49, rebase this branch
onto `main` and point ArgoCD here, or point ArgoCD at this branch now and accept that it also
carries #49.

Then, in order:

1. definition arrives (ArgoCD reconcile took ~180s in practice)
2. apply the example; expect the interpreter to provision and the pod to reach Ready. On a
   first deploy the IAM policy is new, so expect several minutes of `AccessDenied` while it
   propagates and confirm the init loop rides it out instead of giving up
3. confirm the enforced policy with `aws iam get-policy-version` on
   `code-tools-sandbox-iam-policy`, checking for `code-interpreter-custom/agents_code_sandbox-*`
4. `test/e2e-multisession.js` through a port-forward to `svc/code-interpreter-mcp-stable`
5. a gateway test: adapt `.local/browser-mcp-gw-tests/gw-test.js`, changing the path to
   `/mcp/code-interpreter-mcp` and the tool calls to `executeCode`. Note that script asserts
   backend counters through a port-forward that reaches only one pod, so it reports a false
   FAIL at more than one replica
6. drive it through a real agent and confirm the model actually calls the tools

**KubeVela does not re-render an existing Application when only a definition changes.** To
pick up a definition change, delete and re-apply the app.

Two things worth watching specifically, because they are the parts that only on-cluster
testing can exercise: whether `dependsOn: [sandbox]` correctly gates on
`codeInterpreterId != ""`, and whether the measured flat memory profile holds under a real
agent's payload sizes rather than the tiny `print(i)` calls the harness makes.

### 3. README

Configuration table, the nine tools, and how to run the tests. `DESIGN.md` already holds the
reasoning, so the README should be short and operational rather than repeating it.

### 4. PR

Based on `feature/agentcore-browser-mcp`, or `main` once #49 merges, referencing #51. Every
claim verified before it is written. Do not include caveats about not having verified something
because of branch or tooling logistics; if verification is blocked, ask.

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
