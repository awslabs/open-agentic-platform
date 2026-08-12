# Resume here: code-interpreter-mcp

Written 2026-08-11. Read `DESIGN.md` first for what this is and why. This file is only the
state and the remaining work. Issue #51 tracks the overall task.

## State

- **Branch:** `feature/agentcore-code-interpreter`, created off `feature/agentcore-browser-mcp`
  at commit `ae55c35`. It needs that branch's `mcp-server` work (selector-based gateway
  targets with session affinity, optional `replicas`), so do not rebase onto `main` until
  #49 merges.
- **Nothing is committed yet.** All files below are new and untracked. There is no PR.
- **Nothing is deployed.** No OAM component exists for this yet, so nothing runs on-cluster.
- Built and passing locally against the live service in `us-west-2`.

### Files that exist

```
applications/code-interpreter-mcp/
  package.json            deps pinned to browser-mcp's proven versions
  DESIGN.md               architecture, decisions, evidence
  RESUME.md               this file
  src/config.js           env-driven config
  src/log.js              copied verbatim from browser-mcp
  src/agentcore.js        resolve interpreter, start/stop session, invoke + drain stream
  src/tools.js            the 9 tool schemas and allow/deny filtering
  src/session.js          per-MCP-session interpreter, task-aware idle eviction
  src/server.js           StreamableHTTP endpoint, health/ready, reaper, shutdown
  test/e2e-multisession.js
  test/task-eviction.js
spikes/agentcore-code-interpreter/
  probe.mjs               API surface probe (run first, answers the ARN question)
  probe-async.mjs         async task trio probe
  FINDINGS.md             raw measured output from both
```

### How to run what exists

```bash
cd applications/code-interpreter-mcp
npm install
eval "$(aws configure export-credentials --format env)"

# server against the AWS built-in interpreter, no provisioning needed
AWS_REGION=us-west-2 MCP_PORT=8031 node src/server.js

# in another shell
BASE_URL=http://localhost:8031 node test/e2e-multisession.js         # expect E2E PASS

# task eviction needs a short idle window, so run a second server
AWS_REGION=us-west-2 MCP_PORT=8032 SESSION_IDLE_SECONDS=15 node src/server.js
BASE_URL=http://localhost:8032 IDLE=15 node test/task-eviction.js    # expect TASK EVICTION PASS
```

The spikes need no `npm install`; they symlink browser-mcp's `node_modules`.

## Remaining work, in order

### 1. Dockerfile and published image

Copy `applications/browser-mcp/Dockerfile` and strip what does not apply. Keep: pinned
`node:20-alpine` **by digest**, `npm ci --omit=dev`, non-root user, `ENV MCP_PORT/MCP_PATH`,
`EXPOSE 8000`.

Drop: `PUPPETEER_SKIP_DOWNLOAD`, the global `chrome-devtools-mcp` install, and **`tini`**.
`tini` exists in the browser image solely to reap orphaned grandchildren of the
`chrome-devtools-mcp` children. This server spawns no children, so Node as PID 1 is fine.
If that ever changes, add `tini` back.

Publish multi-arch to the same registry as the browser image:

```bash
REG=public.ecr.aws/z0a4o2j5/code-interpreter-mcp
TAG=0.1.0
podman build --platform linux/arm64 -t $REG:$TAG-arm64 -f Dockerfile .
podman build --platform linux/amd64 -t $REG:$TAG-amd64 -f Dockerfile .
aws ecr-public get-login-password --region us-east-1 | podman login --username AWS --password-stdin public.ecr.aws
podman manifest create $REG:$TAG
podman manifest add $REG:$TAG containers-storage:$REG:$TAG-arm64
podman manifest add $REG:$TAG containers-storage:$REG:$TAG-amd64
podman manifest push --all $REG:$TAG docker://$REG:$TAG
skopeo inspect --raw docker://$REG:$TAG        # record the real digest, never type one from memory
```

This machine is **podman only**. Do not use docker.

### 2. The `agentcore-code-interpreter` OAM component

Write `platform/oam/definitions/components/agentcore-code-interpreter.cue`. Model it on
`agentcore-browser.cue`, which already has the shape and the hard-won IAM comment. It must:

- provision `CodeInterpreter` (`bedrockagentcore.aws.upbound.io`). `forProvider` accepts
  `name`, `description`, `region`, `networkConfiguration`, `executionRoleArn`,
  `executionRoleArnRef`, `executionRoleArnSelector`, `tags`; only `region` is required.
- default `region` to `*"{{ .Values.global.awsRegion }}" | string`, never a literal.
- emit an IAM `Policy` named `<appName>-<component>-iam-policy`, which is the naming
  convention `aws-service-identity`'s `accessFor` attaches by.
- declare a `healthPolicy` so `dependsOn` can gate on it, mirroring the browser's
  `isHealth: codeInterpreterId != ""`.

The policy, already validated by simulation (do not re-derive it):

```
Sid Discover:            ListCodeInterpreters                     Resource "*"
Sid UseThisInterpreter:  GetCodeInterpreter, StartCodeInterpreterSession,
                         GetCodeInterpreterSession, ListCodeInterpreterSessions,
                         StopCodeInterpreterSession, InvokeCodeInterpreter
                         Resource arn:aws:bedrock-agentcore:<region>:{{ .Values.global.awsAccountId }}:code-interpreter-custom/<name>-*
```

`awsAccountId` is already plumbed from the cluster secret's `aws_account_id` annotation
through `gitops/addons/bootstrap/default/addons.yaml` and the chart's `values.yaml`, which
falls back to `"*"`. Exclude `CreateCodeInterpreter` and `DeleteCodeInterpreter`: Crossplane
provisions under its own credentials.

Then regenerate and validate:

```bash
cd platform/oam
KUBECONFIG=../../.platform/private/hub-kubeconfig ./generate.sh
cd - && git status --short          # confirm ONLY the intended definitions changed
kubectl --context peeks-hub apply --dry-run=server -f gitops/addons/charts/oam-agent-components/templates/agentcore-code-interpreter.yaml
```

### 3. Example applications

- `platform/oam/examples/example-code-interpreter-mcp.yaml`, modelled on
  `example-browser-mcp.yaml`: the `agentcore-code-interpreter` component plus an
  `mcp-server` component, `aws-service-identity` with `accessFor`, and **no region, account
  or cluster anywhere** in the file.
- Extend or copy `example-agent-with-browser.yaml` to show an agent consuming it with
  `mcpServers: [{name: code-interpreter-mcp}]` and the `gateway-identity` trait.

Set resources on the `mcp-server` component only after step 5 measures the real per-session
cost. Do not copy the browser's `512Mi`/`1Gi`, which are sized for a per-session child
process this server does not have.

### 4. On-cluster verification

ArgoCD currently reconciles `feature/agentcore-browser-mcp`, so **this branch will not
deploy on its own**. `config.local.yaml` (gitignored) is set to `main` but was deliberately
not re-bootstrapped, because `main` lacks #49's definitions. Two options:

- preferred: merge #49, rebase this branch onto `main`, then point ArgoCD at this branch
  with `config.local.yaml` + `task agentic:bootstrap`;
- or point ArgoCD at this branch now, accepting that it also carries #49's changes.

Then verify, in this order:

1. definition arrives (ArgoCD reconcile took ~180s in practice)
2. apply the example, watch the pod reach Ready, expect a fresh IAM propagation delay of
   several minutes on the first deploy and confirm the retry loop rides it out
3. inspect the enforced policy: `aws iam get-policy-version` on
   `<app>-<component>-iam-policy`, confirming `code-interpreter-custom/<name>-*`
4. run `test/e2e-multisession.js` through a port-forward to `svc/code-interpreter-mcp-stable`
5. run a gateway test (adapt `.local/browser-mcp-gw-tests/gw-test.js`, changing the MCP path
   to `/mcp/code-interpreter-mcp` and the tool calls to `executeCode`)
6. drive it through a real agent and confirm the tools are used

**KubeVela does not re-render an existing Application when only a definition changes.** To
pick up a definition change you must delete and re-apply the app. This cost 20 minutes of
confusion on the browser work.

### 5. Measure, then set limits

Measure per-session memory the way the browser was measured, two independent ways:
`kubectl top pod` deltas and `ps -o pid,rss` inside the container, with N sessions held
open. `applications/browser-mcp/test/concurrency-cost.js` is a working harness to adapt: it
opens N sessions, holds them, and reports the backend counters.

Then set `requests`/`limits` from the measurement and reconcile `MAX_INTERPRETER_SESSIONS`
with the memory limit so a full pod is not an OOMKill. Also check whether
`StartCodeInterpreterSession` has the concurrency cliff that CDP attach had; if it does,
gate concurrent activations rather than advertising a cap the pod cannot honour.

### 6. README and PR

`README.md` covering the configuration table, the tools, and how to run the tests. Then a PR
based on `feature/agentcore-browser-mcp` (or `main` once #49 merges) that references #51.
Every claim in the PR must be verified before it is written; do not include caveats about
not having verified something because of branch or tooling logistics. If verification is
blocked, ask rather than shipping the hedge.

## Open questions worth answering

- `sessionTimeoutSeconds` maximum. 900 is used by default because that is the browser's
  AWS-enforced default; the code interpreter's ceiling is unconfirmed.
- Account quota on concurrent code interpreter sessions.
- `sessionId` is typed optional on `InvokeCodeInterpreter`. If omitting it makes the service
  create an implicit session, that is a leak we would never see. The server always passes
  one, so this is about documenting the hazard rather than a live bug.
- `filesystemConfigurations` and `certificates` on session start, unprobed. Likely how
  shared storage is mounted and a private CA trusted.
- Whether `executeCode` with `language: javascript` or `typescript` needs `runtime` set.
  Only python was exercised.

## Gotchas already paid for

- `generate.sh` needs a reachable KubeVela cluster: `KUBECONFIG=.platform/private/hub-kubeconfig`.
- Regenerating no longer damages `agent.yaml`. The `opentelemetry-instrument` command was
  moved into `agent.cue` on the browser branch, so the old "always `git checkout agent.yaml`"
  workaround is obsolete.
- `kubectl get application` resolves to ArgoCD's CRD. Use `applications.core.oam.dev` for OAM.
- The workload is named after the **component**, not the Application.
- Crossplane `CodeInterpreter` and IAM `Policy` are cluster-scoped and will not show up in
  namespaced listings.
- Three custom interpreters already exist in this account
  (`peeks_hub_agent_core_code_interpreter-S8Z5DTeRiG` and the two spokes), provisioned by
  `gitops/addons/charts/crossplane-agentcore`. That chart's XRD and composition are live.
  Do not rebuild the provisioning layer; this component provisions its own interpreter the
  way `agentcore-browser` does, so per-application isolation is preserved.
- Never attach the shell to a long-running server. Run it detached and poll with bounds.
  macOS has no `timeout`; use `perl -e 'alarm N; exec @ARGV' <cmd>`.
- Do not type versions, digests or ids from memory. Paste them from tool output.
