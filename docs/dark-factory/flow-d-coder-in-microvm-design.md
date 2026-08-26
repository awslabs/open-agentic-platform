# Flow D — Running the Coder *inside* the Lambda MicroVM (design)

**Status:** design / spike — NOT implemented. Written after proving the Flow D **substrate**
end-to-end and discovering that running the actual coder in the VM is an application
re-architecture, not a wiring task.

## TL;DR

The Flow D **substrate + lifecycle is proven live**: a `darkfactory-lambda` GitHub issue →
Argo sensor → `df-run` claims the Lambda warm pool → the bridge calls `RunMicrovm` → a real
Lambda MicroVM reaches **RUNNING** in AWS → `suspend`/`resume`/`terminate` are wired → the VM
is terminated on teardown (verified, zero orphans).

What is **NOT** done: the coder does not actually *execute* inside that MicroVM, so no PR is
produced. That is because the one-shot `dark-factory-coder` and the Lambda MicroVM
snapshot/hook execution model are **fundamentally different execution shapes**. Closing the gap
requires re-architecting the coder, plus VPC/Bifrost networking. This doc specifies that work
so it can be decided deliberately.

## Why it isn't just wiring — the execution-model mismatch

| | Kata coder (Flow B, works today) | Lambda MicroVM model |
| --- | --- | --- |
| Shape | **one-shot batch process**: `node entrypoint.js` runs clone→agent→push→PR, then exits | **long-lived HTTP service** that is *snapshotted* at build, *resumed* per session |
| Duration | 5–15 min per run | per-request; the `run` lifecycle hook has a **30s timeout** ("keep it short — on the critical path") |
| Trigger | pod start + `DF_ISSUE_NUMBER` env injected by the SandboxClaim | build-time `ready`/`validate` hooks; per-instance `run` hook receives `runHookPayload` as the HTTP request body |
| Secrets/context | files projected into the pod (`/etc/secrets/gh-token`, `bifrost-api-key`) + `DF_*` env | `runHookPayload` — a **Kubernetes `SecretKeyReference`** on the `Microvm` CR, delivered as the `/run` hook body (≤16 KB); image must set `hooks.microvmHooks.run: ENABLED` |
| Network | in-cluster: reaches Bifrost by ClusterIP `172.20.181.17:8080`; git/gh over public :443 | runs **outside the cluster network**; only `INTERNET_EGRESS` by default; cannot reach a ClusterIP; VPC reach needs an egress **network connector** |

The killer facts (verified against `mmeckes/lambdamicrovms-controller` docs + the live `aws
lambda-microvms`/`lambda-core` CLIs, 2026-08-03):

1. **`/run` hook = 30s timeout.** A 5–15 min coder run cannot happen *in* the hook.
2. **The intended app model is request/response** (02-developer-handoff: RunMicrovm → mint
   auth token → HTTP request → response → terminate) — not a batch job.
3. **`runHookPayload` is delivered as an HTTP body to a `/run` endpoint the app must SERVE** —
   NOT an env var and NOT a mounted file. (An earlier attempt at an env/file boot-shim was
   wrong and is discarded.)
4. So the coder must be **wrapped in a long-running HTTP server** that starts the coding work
   asynchronously — the coder's current `entrypoint.js` is not written that way.

## Proposed design (async `/run` pattern)

Keep the coder *logic* (`entrypoint.js`) intact; change how it is *invoked*.

```
build:  MicrovmImage (FROM arm64 dark-factory-coder + a thin HTTP wrapper)
        hooks.port: 8080
        microvmImageHooks.ready:  server up → safe to snapshot
        microvmHooks.run: ENABLED (30s), suspend/resume/terminate ENABLED

run:    controller delivers runHookPayload (Secret {ghToken, bifrostKey, bifrostUrl(NLB),
        issueNumber, repo, branch, baseBranch, title}) as the /run body
        → wrapper writes /etc/secrets/{gh-token,bifrost-api-key} + exports DF_*/BIFROST_URL
        → wrapper spawns `node entrypoint.js` in the BACKGROUND, returns 200 within 30s
        → coder does clone→agent→push→PR async (many minutes)

observe: df-run's existing `await-coder` step ALREADY polls GitHub for the PR head — reuse it
        verbatim; it doesn't care whether the coder ran in Kata or a MicroVM.

teardown: suspend/resume/terminate hooks best-effort flush; bridge TerminateMicrovm on exit.
```

### Components to build

1. **HTTP wrapper + artifact** (`coder-microvm/`): a small server (`server.js`) exposing
   `ready`, `run`, `suspend`, `resume`, `terminate` on :8080; `run` materializes the payload
   into the coder's existing file/env contract and background-spawns `entrypoint.js`. Dockerfile
   `FROM <ACCOUNT_ID>.dkr.ecr.us-west-2.amazonaws.com/dark-factory-coder:<tag>-arm64`. Zip
   (Dockerfile + server.js) → S3, per the controller's `ci/package-artifact.sh` format.
   *(Supersedes the placeholder `microvm-entry.js` listener that only existed to get the image
   to CREATED.)*

2. **MicrovmImage: enable hooks** (RGD `templates/image/10-rgd-and-image.yaml`): add
   `hooks.port: 8080`, `microvmImageHooks.ready: ENABLED`, `microvmHooks.run/suspend/resume/
   terminate: ENABLED`. Without `run: ENABLED` the payload is silently never delivered.

3. **VPC egress connector** (bootstrap Job — honest: *GitOps-provisioned, not continuously
   reconciled*; no ACK/Crossplane API exists for `lambda-core` connectors). Committed
   find-or-create Job modeled on `06-securityagent-bootstrap.yaml`:
   `aws lambda-core get/create-network-connector` with
   `VpcEgressConfiguration={SubnetIds:[hub subnets], SecurityGroupIds:[sg], NetworkProtocol:IPv4}`
   → writes the connector ARN to a ConfigMap the MicrovmImage `egressNetworkConnectors` reads.
   IAM: the bootstrap/capability role needs `lambda-core:*NetworkConnector*` + the EC2 ENI perms
   Lambda uses to provision ENIs. **Caveat:** if the connector is deleted out-of-band, nothing
   self-heals until the Job re-runs (not a controller).

4. **Bifrost VPC-reachable** (internal NLB — this part *is* declarative): a `Service
   type=LoadBalancer` with `service.beta.kubernetes.io/aws-load-balancer-internal: "true"` +
   `nlb-target-type` in the bifrost chart, reconciled by the AWS Load Balancer Controller. The
   MicroVM (via the egress connector) reaches Bifrost at the NLB's stable VPC address on :8080.
   (Bifrost's pod IP `10.0.x.x` is in-VPC and reachable via the connector, but ephemeral — the
   NLB gives a stable target. Its ClusterIP `172.20.x.x` is NOT routable from a VPC ENI.)
   **Shared-infra change — needs owner sign-off.**

5. **runHookPayload Secret + Microvm wiring**: the bridge (or a per-session step) writes a
   Secret with the payload key and the `Microvm`/RunMicrovm references it as
   `runHookPayload: {name, key}`. Since it's a SecretKeyReference the **controller** delivers it
   — confirm whether the imperative `RunMicrovm` path the bridge uses accepts the same, or
   whether this session should create a short-lived `Microvm` CR instead.

6. **Security-group rules**: allow the connector ENIs → Bifrost NLB on :8080.

## Open questions for review

- **Async vs request-driven?** Background-spawn (df-run polls for the PR, minimal coder change)
  vs. the reference's request/response model (bridge sends an HTTP "code this" request + waits;
  needs the auth-token path). Background-spawn reuses `await-coder` and is less invasive.
- **Imperative RunMicrovm vs a `Microvm` CR per session?** `runHookPayload` being a
  SecretKeyReference is controller-delivered; the current bridge calls `aws run-microvm`
  imperatively. Decide whether per-session VMs become short-lived `Microvm` CRs (declarative
  payload delivery) or stay imperative (verify the CLI accepts an inline/secret payload).
- **Cost:** the VPC egress connector provisions ENIs; the internal NLB is an hourly resource.
  Both are ongoing while Flow D is enabled.
- **Is in-VM coder even required for the goal?** The substrate is a valid deliverable on its
  own (a second sandbox substrate). Running the coder in it is the "make it actually code" step
  — worth confirming it's in scope before the re-architecture.

## 2026-08-03 build attempt — where it got to + the confirmed blocker

Built and deployed the Bedrock-direct async design end-to-end. Live results:

- ✅ **lambda-coder artifact + image**: `examples/dark-factory/coder-microvm/` (hook-server.js
  + Dockerfile FROM the arm64 coder + the USE_BEDROCK entrypoint branch). MicrovmImage rebuilt
  to **UPDATED** with `hooks` enabled; exec role has `bedrock:InvokeModel*`. Verified the
  hook-server runs in the VM — CloudWatch shows `[hook-server] listening on :8080 (lambda-coder,
  Bedrock-direct)`.
- ✅ **Bridge payload**: builds JSON (issue ctx + GitHub token + region) with python3 (node is
  absent in the aws-cli image) and passes `--run-hook-payload` on `run-microvm`. VM launches
  RUNNING with the payload; no bridge crash.
- ❌ **BLOCKER: the `/run` hook never fires** → the coder never starts in the VM → no PR.
  CloudWatch shows the server `listening` but never logs the `/run` handling / background-spawn.

**Confirmed root cause:** `runHookPayload` is a **`SecretKeyReference`, "not a literal"** — the
docs + the 02-developer-handoff example deliver it via the **declarative `Microvm` CR**
(`runHookPayload: {name, key}` → the self-managed controller reads the Secret and drives the
`/run` hook). The imperative `run-microvm --run-hook-payload "<string>"` CLI path the bridge uses
does **not** invoke `/run` (VM reaches RUNNING but the hook is silent). Two things also worth
noting from the reference: (a) the intended session model is the CLIENT minting an auth token and
sending an HTTP request to the VM endpoint with `X-aws-proxy-auth` (request/response), and (b) the
`/run` hook is service-internal on VM start.

**Correct path (next):** make the per-session VM a **`Microvm` CR** (declarative), not a bridge
CLI call:
- bridge (or a per-session step) writes a **Secret** with the payload key, then creates a
  `Microvm` CR referencing it (`imageIdentifierRef`, `executionRoleRef`, `runHookPayload:{name,key}`,
  `idlePolicy`), and reads back `status.microvmID` for the lifecycle annotation.
- the self-managed lambdamicrovms controller reconciles it and delivers the payload to `/run`,
  which background-spawns the coder.
- teardown = delete the `Microvm` CR (controller terminates), replacing the imperative
  TerminateMicrovm.
This trades the imperative bridge for the declarative CR path the payload mechanism actually
requires — and it's MORE GitOps-faithful. Est: bridge rewrite (CR create/delete instead of CLI)
+ a per-session Secret; the hook-server/artifact/image/IAM/Bedrock pieces are already done and verified.

## 2026-08-03 (later) — declarative Microvm CR path: reconciles + VM runs, /run still silent

Switched the bridge from the imperative `run-microvm` CLI to the **declarative `Microvm` CR**
path (write payload Secret → create `Microvm` CR with `runHookPayload:{name,key}` → controller
reconciles → delete CR on teardown). Verified working:
- ✅ Bridge creates the Secret + `Microvm` CR (`mvm-<issue>`); RBAC for microvms+secrets added.
- ✅ Controller reconciles it: CR `state=RUNNING`, `ACK.ResourceSynced=True`, `status.microvmID`
  populated, annotated on the Sandbox. Deleting the CR cleanly terminates the VM (0 orphans).
- ✅ MicrovmImage is v2.0, `UPDATED`, `hooks` present (run/ready/suspend/resume/terminate).
- ❌ **STILL no `/run` output**: CloudWatch `/aws/lambda/microvms/coder-image` shows the build-time
  `[hook-server] listening on :8080` but ZERO runtime events after the VM starts — the coder never
  logs, no PR. The `/run` hook is not producing coder execution we can observe.

**What's ruled out:** payload delivery mechanism (now declarative CR, the documented path), image
hooks (present, v2.0 built), IAM (bedrock on exec role), bridge crash (restarts=0), YAML (renders
clean). **What's NOT yet proven:** that the service actually invokes `/run` against the hook-server,
and that hook-server's `/run` handler + background-spawn + Bedrock call execute. Can't see inside
the VM beyond CloudWatch (which is empty at runtime) — needs either (a) the VM's runtime logs routed
somewhere visible, (b) hitting the VM endpoint directly with an auth token (X-aws-proxy-auth) to
probe the hook-server, or (c) the controller/service confirming the run-hook HTTP call + its response.
This is the current debugging frontier — the substrate, image, CR path, and teardown all work; the
open question is purely whether/how the `/run` hook reaches the in-VM hook-server and why it emits
no logs.

## 2026-08-03 E2E run #106 — full chain works to /run; 2 pinpointed gaps

Ran a clean GH-issue E2E and **probed the VM directly** (minted an auth token, hit the endpoint).
Stage-by-stage: issue → label → workflow → bridge claim → Microvm CR (`mvm-106`) → VM RUNNING with
endpoint — all ✅. Then the decisive probes against the live VM:
- `GET https://<endpoint>/` (X-aws-proxy-auth) → `{"status":"ok","path":"/"}` → **hook-server is
  ALIVE and reachable at runtime.**
- `POST /run` → `{"status":"started"}` → **the /run handler works and background-spawns the coder.**

So the entire chain — including the hook-server and its /run→coder-spawn — is functional. The two
remaining gaps are now precisely isolated:

1. **The service does not auto-invoke `/run` on launch.** After RunMicrovm/Microvm-CR reconcile, the
   run hook is not called automatically — I had to POST /run manually to start the coder. Either the
   run hook fires on a trigger we're not hitting, or the payload/hook wiring needs a specific field to
   auto-fire. (auth-token minting: `create-microvm-auth-token --expiration-in-minutes N --allowed-ports
   port=8080`; token is at `.authToken.X-aws-proxy-auth`.)
2. **Runtime logs don't reach CloudWatch.** `logging.cloudWatch.logGroup` on the image captures BUILD
   logs only; after the VM runs, `/aws/lambda/microvms/coder-image` has 0 runtime events even though the
   hook-server clearly runs (proven by the probe). This blinded every prior run — need to wire runtime
   stdout/stderr to CloudWatch (or read it another way) to observe the coder.

Both are now concrete, small-surface problems (a hook-trigger config + a log-routing config), NOT
architecture. The substrate, image+hooks, Bedrock exec role, declarative Microvm CR path, payload
delivery, hook-server, /run→coder-spawn, and clean CR-delete teardown are all verified working.

## What exists today (so nothing is lost)

- Substrate live: RGD Active, S3 bucket, build/exec roles, **MicrovmImage CREATED (v1.0)**,
  bridge launches/terminates a real MicroVM from a `darkfactory-lambda` issue.
- All the substrate + bridge fixes are committed on `flow-d-lambda-microvm-sandbox` (container
  named `coder`, aws-cli v2 image, kubectl fetch, API-server + Pod Identity egress,
  downward-API SANDBOX_NAME, microvmSuspend on, `darkfactory-lambda` label).
- The **placeholder** code artifact (`microvm-entry.js` listener) is what's in S3 today — it
  only proved the image builds; it must be replaced per §1 above.
