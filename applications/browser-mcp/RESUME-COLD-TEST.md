# Resume here: cold-state test of browser-mcp 0.1.3

Written 2026-08-10. Read this, then run the test in "The test to run" below.
Background and prior results live in `applications/browser-mcp/DESIGN.md`; this file
is only what is needed to continue.

## Current state

- **Branch:** `feature/agentcore-browser-mcp`, clean, pushed, HEAD `1fc7c30`.
- **PR #49** open, base `feature/oam-for-agents` (stacked on PR #36). PR #36 is
  unblocked and conflict-free.
- **ArgoCD tracks `feature/agentcore-browser-mcp`** (set in `config.local.yaml`
  `agenticRepo.revision`, applied via `task agentic:bootstrap`). Remember to flip it
  back or merge, otherwise deployed state diverges from `main` indefinitely.
- **Nothing is deployed right now.** The `browser-tools` Application, its pod, its
  Crossplane `Browser` and its IAM policy + role attachment were all deleted. This
  is the clean state the test needs, so do NOT deploy anything before starting.
- Published image: `public.ecr.aws/z0a4o2j5/browser-mcp:0.1.3`
  (digest `sha256:232bcddee221693976692c2a3f9ad3b0a5cd0c189b7b8d72cbf9def668c4644a`).
  The example already references `0.1.3`.

## Why this test exists

Three deploys, three behaviours. The bug moved rather than disappearing, so the test
has to distinguish all three:

| Version | Behaviour on a cold deploy | Verdict |
|---|---|---|
| 0.1.1 | Did slow AWS init BEFORE binding the port, so nothing answered `/healthz`. Liveness (initialDelay 10s, period 30s, failureThreshold 3, no startupProbe) killed it at ~100s. Restarted twice, exit 137. | broken |
| 0.1.2 | Binds port first, so it survived to 264s with 0 restarts. But when `BROWSER_READY_TIMEOUT_SECONDS=300` expired it logged "staying unready" and **stopped retrying**, sitting Running + permanently NotReady until a human deleted it. Worse than crash-looping, and violates the customer bar. | broken differently |
| 0.1.3 | Initialisation retries forever (outer loop, `INIT_RETRY_SECONDS=15` between cycles, no terminal state). Should stay alive AND reach Ready by itself. | **to be proven** |

**What is already proven for 0.1.3:** locally, against a browser that does not exist
with a 10s budget, it logged 3 failed cycles and kept retrying, `/healthz` stayed
200, and `/readyz` reported
`{"status":"initializing","failedCycles":3,"lastError":"...not present in ListBrowsers yet"}`.

**What is NOT proven, and is the whole point of this test:** self-healing against a
real multi-minute AWS delay on-cluster. The previous 0.1.3 deploy reused an existing
IAM policy, so it initialised in 1s and never entered the failure path. I wrongly
presented that as confirmation; it was not.

## The mechanism that creates the delay

Deleting the Application destroys the IAM `Policy` and the `RolePolicyAttachment`.
Redeploying creates brand new ones, and AWS IAM takes minutes to propagate, during
which the pod's role gets:

```
AccessDenied: User: arn:aws:sts::929819487611:assumed-role/browser-mcp-role/...
is not authorized to perform: bedrock-agentcore:ListBrowsers
```

Observed at roughly 5 minutes on the previous cold deploy. This is a first-deploy
cost only; it does not recur while the policy exists. That is exactly why the test
must start from a deleted Application, not from a redeploy.

## The test to run

Cluster context is `peeks-hub`. Everything below is namespace `default`.

**1. Confirm clean state first** (all four must be zero):

```bash
kubectl --context peeks-hub get applications.core.oam.dev -A | grep -c browser-tools
kubectl --context peeks-hub get pods -n default -l app.kubernetes.io/name=browser-mcp --no-headers | wc -l
kubectl --context peeks-hub get policies.iam.aws.upbound.io --no-headers | grep -c browser-tools
kubectl --context peeks-hub get rolepolicyattachments.iam.aws.upbound.io --no-headers | grep -c browser-mcp-web-browser
```

If anything is non-zero, delete and wait for cleanup:
`kubectl --context peeks-hub delete -f platform/oam/examples/example-browser-mcp.yaml`

**2. Deploy:**

```bash
kubectl --context peeks-hub apply -f platform/oam/examples/example-browser-mcp.yaml
```

**3. Watch for up to ~15 minutes.** Poll every 20s and record, per poll:

- `restarts` and `ready` from the pod's containerStatuses
- count of `"will retry"` lines in the pod log (failed init cycles)
- presence of a `"browser-mcp ready"` log line

Pass criteria, all three required:

- **restarts stays 0** for the whole run. Any restart means the liveness split
  regressed (that was the 0.1.1 failure).
- **it reaches `ready=True` on its own**, with no delete, no rollout restart, no
  human action. If it is still NotReady after ~15 minutes with cycles increasing,
  that is still better than 0.1.2 but the retry cadence needs revisiting.
- ideally **`failedCycles >= 1`** before ready, which is the evidence it actually
  entered the failure path rather than sailing past it. If it goes ready in ~1s with
  0 failed cycles, IAM propagated too fast and **the test proved nothing**, same
  mistake as last time. Say so plainly and re-run after deleting again.

Useful observation commands:

```bash
POD=$(kubectl --context peeks-hub get pods -n default -l app.kubernetes.io/name=browser-mcp -o jsonpath='{.items[0].metadata.name}')
kubectl --context peeks-hub logs "$POD" -n default | grep -E "listening|will retry|browser-mcp ready"
kubectl --context peeks-hub get pod "$POD" -n default --no-headers
```

Expected log arc: `listening; initialising (not ready yet)` → zero or more
`Initialisation attempt failed; will retry` → `browser-mcp ready ... cycles: N`.

The image has no `curl`, so read `/readyz` via port-forward, not `kubectl exec`:

```bash
kubectl --context peeks-hub port-forward -n default svc/browser-mcp-stable 8099:80 &
curl -s localhost:8099/readyz
```

**4. Once Ready, re-run all three suites.** They pass currently; this confirms the
retry work did not regress anything.

```bash
TOKEN=$(.local/oap-curl.sh token)
kubectl --context peeks-hub port-forward -n default svc/browser-mcp-stable 8099:80 &
# wait for /readyz to return 200 before starting

cd applications/browser-mcp
BASE_URL=http://localhost:8099 node test/e2e-multisession.js      # expect E2E PASS

# gateway tests: copies kept in .local/browser-mcp-gw-tests/ (gitignored, survives /tmp).
# Copy into the package dir so they resolve node_modules, then delete.
cp ../../.local/browser-mcp-gw-tests/gw-test.js gw-test.mjs && cp ../../.local/browser-mcp-gw-tests/gw-iso.js gw-iso.mjs
GW_URL="https://agents.peeks.dev.shapirov.people.a2z.com" MCP_PATH="/mcp/browser-mcp" \
  DIRECT_URL="http://localhost:8099" TOKEN="$TOKEN" node gw-test.mjs   # GATEWAY PASS
GW_URL="https://agents.peeks.dev.shapirov.people.a2z.com" MCP_PATH="/mcp/browser-mcp" \
  DIRECT_URL="http://localhost:8099" TOKEN="$TOKEN" node gw-iso.mjs    # GATEWAY ISOLATION PASS
rm -f gw-test.mjs gw-iso.mjs
```

If those copies are missing, they are small: connect through the
gateway with `StreamableHTTPClientTransport(url, { requestInit: { headers: {
Authorization: 'Bearer '+TOKEN } } })`, call `navigate_page` then `take_snapshot`,
assert the text contains "Example Domain", and read the backend's `/readyz` through
the port-forward to assert `liveBrowserSessions` went 0 -> 1. The isolation variant
opens two such clients and asserts the backend reports 2 sessions and 2 browsers.

## Gotchas that cost time already

- `kubectl get application` resolves to **ArgoCD's** CRD on this cluster. Use
  `kubectl get applications.core.oam.dev <name> -n <ns>` for OAM.
- The pod is named after the **component** (`browser-mcp`), not the Application
  (`browser-tools`). Searching for `browser-tools` in `default` finds only the
  Application.
- The Crossplane `Browser` and IAM `Policy` are **cluster-scoped**, so they do not
  appear in namespaced listings.
- Never attach the shell to a long-running process. Run detached, poll with bounds.
  macOS has no `timeout`; use `perl -e 'alarm N; exec @ARGV' <cmd>`.
- Each cold deploy creates a **new** browser id (e.g. `agents_web_browser-PqyahPnWEj`
  then `agents_web_browser-XZ8Aazris5`). Do not hardcode it in checks.

## After the test

If it passes, remaining work on PR #49:

1. `agent` component still carries `browser`/`codeInterpreter` parameters that only
   exist in the generated `agent.yaml`, not in `agent.cue`. Blocked on **issue #50**
   (the hand-added `opentelemetry-instrument` command that regeneration deletes).
   Fix #50 and this falls out of a normal `generate.sh` run.
2. Wildcard IAM policy on the AgentCore components (`bedrock-agentcore:*` on `*`)
   should be scoped to the browser ARN.
3. `decentralized-observability-identity.yaml` still has no CUE source.
4. Decide `replicas` policy. Session state is in-memory and pod-local so `replicas: 1`
   is required today; `platform/oam/examples/kgateway-session-affinity.yaml` may be
   the path to scaling out.
5. Flip `config.local.yaml` revision back to `feature/oam-for-agents`, or merge #49.
