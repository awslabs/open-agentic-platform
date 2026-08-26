# Enabling Flow D (Lambda MicroVM substrate) on your own account

Flow D ships **disabled** (`microvm.enabled=false`) and, unlike Flow A/B, it cannot be turned on by
GitOps alone. Three artifacts have to be produced **by hand, per AWS account**, and two of them
cannot be templated from cluster annotations the way the rest of this repo does it. This page is the
end-to-end enablement path — follow it top to bottom.

> **Prerequisites:** Managed KRO + Managed ACK capabilities ACTIVE on the hub (they are EKS
> Capabilities, provisioned in the platform repo — see `docs/EKS-Capabilities-KRO-ACK-Setup.md`), the
> self-managed `ack-lambdamicrovms` addon synced, a Docker host with `buildx`, and admin on the
> GitHub repo the factory will work on. Examples use account `<ACCOUNT_ID>`, region `us-west-2`, repo
> `<OWNER>/<REPO>` — substitute your own.
>
> **Lambda MicroVM is ARM_64-only.** The service model (`lambda-microvms`, API 2025-09-09) defines
> `Architecture` with exactly one enum value, `['ARM_64']`, and it is required. Note the `MicrovmImage`
> CRD declares `architecture` as a bare string with **no** enum, so the API server will happily admit
> `X86_64` and you only discover the problem as an opaque AWS-side build failure. There is no x86 path.

---

## Placeholders you must replace

This repo ships Flow D with **deliberate placeholder values** rather than a working default, so that a
mis-configured deploy fails loudly instead of silently pointing at someone else's account or repo.
Every one of these must be replaced before Flow D works. Nothing here can be injected from cluster
annotations — that is what makes them manual.

| Placeholder | File / location | Replace with |
|---|---|---|
| `REPLACE_ME_ACCOUNT_ID` | `examples/dark-factory/coder-microvm/Dockerfile` — `ARG CODER_IMAGE` | your 12-digit AWS account id (the one holding the ECR repo) |
| `REPLACE_ME_REGION` | same line | your region, e.g. `us-west-2` |
| `replace-me-owner` | `gitops/addons/charts/dark-factory/values.yaml` — `trigger.argoEvents.repositories[].owner` | your GitHub org or user |
| `replace-me-repo` | same block — `.names[]` | your repo name |
| `replace-me-owner-replace-me-repo` | **directory** `gitops/addons/charts/dark-factory/holdout/` | rename to `<owner>-<repo>`, matching the two values above **exactly** |
| `replace-me-owner/replace-me-repo` | inside that directory's `scenarios.json` (`_comment`) and `rubric.md` (heading) | your `owner/repo` — cosmetic, but the `require()` paths in the same file are **not** (see Part D4) |

> **Two casing conventions, on purpose.** `REPLACE_ME_ACCOUNT_ID` is free text inside a Dockerfile, so
> it is loud and uppercase. `replace-me-owner` / `replace-me-repo` are lowercase and hyphenated because
> the holdout slug is interpolated into a **Kubernetes object name**
> (`df-holdout-{{ $slug | lower }}` in `50-holdout-configmap.yaml`), and underscores are invalid in
> RFC 1123 names — an uppercase/underscore placeholder there renders a manifest that cannot be applied.
> Keep any replacement DNS-label-safe for the same reason.

The directory name and the two values are **coupled**: `50-holdout-configmap.yaml` derives the fixture
path from `<owner>-<name>`, and `.Files.Get` returns an **empty string** for a missing file instead of
failing the render. Change one without the other and you get a ConfigMap with an empty
`scenarios.json` — the holdout gate then grades every run against nothing.

Find anything you missed:

```bash
grep -rniE 'replace[-_]me' --include='*.yaml' --include='*.json' --include='*.md' \
  gitops/ examples/ | grep -v docs/
find gitops examples -iname '*replace*me*'      # the fixture directory itself
```

---

## Part A — Build and push the arm64 coder base image

The code-artifact ZIP's Dockerfile does `FROM <account>.dkr.ecr.<region>.amazonaws.com/dark-factory-coder:<tag>-arm64`.
That base image must exist **in the account you are deploying to**.

> **Cross-account pull does not work.** ECR cross-account requires *both* an identity grant and a
> repository resource policy on the far side. The build role's `ecr:BatchGetImage` on `*` is only the
> identity half, so pointing at another account's registry fails with
> `AccessDeniedException ... no resource-based policy allows the ecr:DescribeImages action`.

### A1. Register QEMU if you are building on x86_64

`docker buildx` will accept `--platform linux/arm64` and pull arm64 layers, and `COPY`/`WORKDIR`
succeed — but any `RUN` step dies with `exec /bin/sh: exec format error`, because containers share the
host kernel. The `PLATFORMS` column in `docker buildx ls` lists what the worker can **execute**, not
what it can address.

```bash
docker run --privileged --rm tonistiigi/binfmt --install arm64   # registers qemu-aarch64
ls /proc/sys/fs/binfmt_misc/                                     # expect: qemu-aarch64
# revert later with: docker run --privileged --rm tonistiigi/binfmt --uninstall arm64
```

Emulated builds work but are slow (the `apk add` + `npm install -g` steps take minutes). A native
arm64 builder — e.g. a Graviton node the cluster already has — is faster if you have one.

### A2. Build, verify, push

```bash
docker buildx build --platform linux/arm64 \
  -t dark-factory-coder:v0.2.5-arm64 --load examples/dark-factory/coder

# VERIFY BEFORE PUSHING — the ECR repo is IMMUTABLE, so a tag can only be pushed once.
docker image inspect dark-factory-coder:v0.2.5-arm64 \
  --format '{{.Architecture}} {{.Os}} user={{.Config.User}}'        # expect: arm64 linux user=1000
docker run --rm --platform linux/arm64 --entrypoint sh \
  dark-factory-coder:v0.2.5-arm64 -c 'uname -m; node -v; go version'  # expect aarch64

aws ecr get-login-password --region us-west-2 \
  | docker login --username AWS --password-stdin <ACCOUNT_ID>.dkr.ecr.us-west-2.amazonaws.com
docker tag  dark-factory-coder:v0.2.5-arm64 <ACCOUNT_ID>.dkr.ecr.us-west-2.amazonaws.com/dark-factory-coder:v0.2.5-arm64
docker push <ACCOUNT_ID>.dkr.ecr.us-west-2.amazonaws.com/dark-factory-coder:v0.2.5-arm64

# Confirm from the registry side, not the push output:
docker manifest inspect --verbose <ACCOUNT_ID>.dkr.ecr.us-west-2.amazonaws.com/dark-factory-coder:v0.2.5-arm64 \
  | grep -A2 '"platform"'      # expect: "architecture": "arm64", "os": "linux"
```

Preserve `USER 1000` and `ENV WORKSPACE` from `examples/dark-factory/coder/Dockerfile`. Rebuilding the
image from a public base instead is tempting but drops both, and would run the hook server as root.

---

## Part B — Edit the ZIP's Dockerfile (the one thing GitOps cannot inject)

`examples/dark-factory/coder-microvm/Dockerfile` ships as
`ARG CODER_IMAGE=REPLACE_ME_ACCOUNT_ID.dkr.ecr.REPLACE_ME_REGION.amazonaws.com/dark-factory-coder:v0.2.5-arm64`
and **must be hand-edited** before you build the ZIP in Part C.

> **Why it cannot be templated.** `MicrovmImage.spec.codeArtifact` has exactly **one** property, `uri`.
> There is no build-args field — `spec.environmentVariables` is *runtime* env for the VM, not docker
> build args. So nothing in Helm, the addon registry, or the CR can reach inside the ZIP. Whatever the
> `ARG` default says is what Lambda pulls. This is the one ECR reference in the repo that the
> `{{.metadata.annotations.aws_account_id}}` pattern cannot fix.

Verify the CRD yourself if you doubt it:

```bash
kubectl get crd microvmimages.lambdamicrovms.services.k8s.aws -o json \
  | jq '.spec.versions[0].schema.openAPIV3Schema.properties.spec.properties.codeArtifact.properties'
# => { "uri": { ... } }   — nothing else
```

---

## Part C — Let ACK create the artifact bucket, *then* upload

The KRO RGD creates the bucket as `<image.name>-microvm-artifacts-<accountId>`. The account suffix is
required: S3 bucket names are **one global namespace**, so an un-suffixed `coder-microvm-artifacts` is
first-come-first-served and will already be owned by someone else — `CreateBucket` returns
`409 BucketAlreadyExists`, the `Bucket` CR never gets an ARN, and the image build fails with
`Access denied when fetching artifact from S3` → `CREATE_FAILED`.

> **Ordering matters — do not pre-create the bucket.** ACK refuses to manage a bucket it did not
> create: the CR parks at `ACK.Terminal=True`, *"This resource already exists but is not managed by
> ACK ... enable the ResourceAdoption feature gate and populate the
> `services.k8s.aws/adoption-policy` annotations."* On Managed ACK (which runs off-cluster) that gate
> is not yours to enable. Sync the chart first, wait for the `Bucket` CR to reach
> `ACK.ResourceSynced=True`, and only then upload. Builds still work with a `Terminal` bucket CR, but
> you are left with a permanently unhealthy resource.

```bash
kubectl get bucket -n agent-sandbox-system \
  -o custom-columns='NAME:.spec.name,SYNCED:.status.conditions[?(@.type=="ACK.ResourceSynced")].status'
```

Then assemble and upload the ZIP. **No script in this repo does this** — the ZIP and the base image
have only ever been built by hand, which is the root cause of this whole class of drift.

```bash
cd examples/dark-factory/coder-microvm
cp ../coder/entrypoint.js .            # overlays the (newer) coder over the one baked in the base
zip -X /tmp/coder-v0.2.5-arm64-r8.zip Dockerfile hook-server.js entrypoint.js
unzip -p /tmp/coder-v0.2.5-arm64-r8.zip Dockerfile | grep ARG   # confirm YOUR account id is inside
aws s3 cp /tmp/coder-v0.2.5-arm64-r8.zip \
  s3://coder-microvm-artifacts-<ACCOUNT_ID>/coder-v0.2.5-arm64-r8.zip --region us-west-2
```

Set `microvm.codeArtifactKey` to that **object key only** — the bucket half is composed by the RGD
from the bucket it creates (`s3://${bucket.spec.name}/${schema.spec.codeArtifactKey}`), so bucket, IAM
grant, and artifact URI all derive from one expression. **Bump the key to rebuild**: Lambda does not
rebuild when the same S3 key is overwritten.

---

## Part D — GitHub: repo target, label, credentials

### D1. Point the EventSource at your repo

`trigger.argoEvents.repositories` in `charts/dark-factory/values.yaml` ships the placeholders
`replace-me-owner` / `replace-me-repo`, which match no real repo. Until you replace them Argo Events
cannot register the webhook and logs, every 60s:

```
failed to list existing webhooks of <owner>/<repo>. err: GET .../hooks: 404 Not Found
```

A 404 here means *either* the repo does not exist *or* the token cannot see it — GitHub deliberately
does not distinguish. This value is **load-bearing twice**: `50-holdout-configmap.yaml` derives the
holdout fixture path from it (Part D4).

### D2. Create the trigger label

The Lambda substrate is triggered by the **`darkfactory-lambda`** label (Kata uses `dark-factory`).
It does not exist on a fresh repo:

```bash
gh label create darkfactory-lambda --repo <OWNER>/<REPO> \
  --description "Hand this issue to the Dark Factory coder running in a Lambda MicroVM (Flow D)" \
  --color 5319E7
```

### D3. The three credentials

Full Secrets Manager setup is in [README §10a](README.md#10a-github-credentials-secrets-manager-setup).
Flow-D-specific notes:

- The **events** token needs `admin:repo_hook` on the target repo — Argo Events *self-registers* the
  webhook, and a read-only token cannot. Confirm the token can see the repo at all:
  `GET /repos/<OWNER>/<REPO>/hooks` should return 200, not 404.
- After a successful sync, verify the hook exists and that a delivery actually **authenticates** —
  a `code=200` proves the cluster's `webhook-secret` matches the one on the hook (GitHub never
  returns a hook's secret, so this is the only way to check):
  ```bash
  gh api repos/<OWNER>/<REPO>/hooks --jq '.[]|{id,active,url:.config.url,events}'
  gh api -X POST repos/<OWNER>/<REPO>/hooks/<HOOK_ID>/pings
  gh api repos/<OWNER>/<REPO>/hooks/<HOOK_ID>/deliveries --jq '.[0]|{event,status,status_code}'
  ```

> **Single-account caveat.** `iterate.js` skips any comment whose author equals the **orchestrator**
> token's identity (`GET /user`) — the self-trigger guard. If the orchestrator PAT is your own
> personal token, **you cannot drive a human fix round from your own account**: every comment you
> post looks like the factory talking to itself. Use a second GitHub identity for review comments, or
> submit the fix round directly (Part F).

### D4. Add a holdout fixture for your repo

`holdout/<owner>-<repo>/{scenarios.json,rubric.md}` is read by `.Files.Get`, which returns **empty
string** for a missing file rather than failing. The repo ships
`holdout/replace-me-owner-replace-me-repo/` — **rename it** to match your `owner`/`repo` values — so a repo with no fixture renders a ConfigMap with
empty `scenarios.json` and the gate grades every run against nothing.

The fixture's `require()` paths must match **your** repo's layout. The shipped fixture requires
`$REPO/app/index.js`; against a root-level `index.js` every test throws `MODULE_NOT_FOUND`. That is
worse than a skip, because `appliesWhen` tests the **PR diff**, not file existence — the scenarios
become *applicable*, all error, and the gate hard-fails at 0%. Do **not** "fix" this by flipping
`appliesWhen` to false: `evaluate.js` warns that turning the mismatch into a SKIP makes the gate go
**green over a diff it never evaluated**, indistinguishable from a real pass.

---

## Part E — Enable, sync, and trigger

### E1. Opt in (nothing renders without this)

Flow D is **off by default**. One label gates the whole stack — the pipeline chart, the Lambda MicroVM
substrate, and the pre-GA `lambdamicrovms` ACK controller:

```yaml
# gitops/overlays/environments/control-plane/enabled-addons.yaml
enabledAddons:
  dark_factory: true
```

Skip this and none of the three ArgoCD Applications are created at all — no error, they simply never
appear, because the registry entries select on `enable_dark_factory`.

`dark_factory` is not sufficient on its own — **`agent_sandbox: true` is also required**, even for a
Lambda-only deployment:

```yaml
  agent_sandbox: true        # REQUIRED by Flow D too — see below
```

The `agent-sandbox-operator` (gated by `enable_agent_sandbox`) is the only thing that installs the
`Sandbox*` CRDs, and the Lambda substrate itself renders a `SandboxTemplate` (the bridge) plus a
`SandboxWarmPool`. Neither of those CRs carries `SkipDryRunOnMissingResource`, and ArgoCD computes an
app's whole diff before applying any sync wave, so with the CRDs absent the `agent-sandbox-lambda`
Application fails at comparison — the same shape as the ComparisonError described in E2 below, and it
does **not** self-heal. The operator is cheap: CRDs plus one controller Deployment, no EC2 capacity.

The **Kata** runtime flags are genuinely optional for Flow D, and only needed for `df-run` (Flow A/B),
which claims from the Kata warm pool:

```yaml
  agent_sandbox_kata: true   # the clh/qemu runtime binaries on the node (kata-deploy)
  kata_nodepool: true        # dedicated Karpenter NodePool for nested-virt nodes
```

Enable those two **together** or not at all: `kata_nodepool` alone leaves a NodePool idle at 0 nodes,
and the runtime alone gives you a handler with no node to run on. Note the split — `kata-deploy` ships
the runtime binaries but is configured `runtimeClasses.enabled: false`, so the three RuntimeClasses
(`kata-clh`, `kata-fc`, `kata-qemu`) come from the `agent-sandbox` chart instead. Both halves are
needed for a working Kata sandbox.

⚠️ **Flow-D-only clusters:** `agent_sandbox: true` also renders the Kata `SandboxWarmPool`
(`coder-warmpool`, `warmPool.targetIdle: 1`). Its pods pin `nodeSelector
katacontainers.io/kata-runtime=true` and tolerate the `kata` taint, which only `kata-nodepool` creates —
so without the two Kata flags that one warm pod sits **Pending** indefinitely. Harmless but untidy; set
`warmPool.enabled: false` in a per-cluster `agent-sandbox` overlay if you want Flow D with no Kata path.

### E2. Point the substrate at your image

```yaml
# clusters/<cluster>/agent-sandbox-lambda/values.yaml
microvm:
  enabled: true
  baseImageARN: "arn:aws:lambda:us-west-2:aws:microvm-image:al2023-1"
  codeArtifactKey: "coder-v0.2.5-arm64-r8.zip"   # object key ONLY
```

`accountId` and `podIdentity.clusterName` are injected from the cluster's `aws_account_id` /
`aws_cluster_name` annotations by the addon registry — leave them unset per cluster. The RGD instance
`require`s `accountId`, so a missing value fails the render loudly instead of emitting an invalid
trailing-hyphen bucket name.

> **⚠️ Upgrading an existing cluster: CRD/instance ordering deadlock.** Adding a new field to the RGD
> schema while the same sync also sets it on the instance can wedge the Application:
> ```
> ComparisonError: ... error building typed value from config resource:
>   .spec.accountId: field not declared in schema
> ```
> ArgoCD computes the whole app's diff **before** applying any sync wave, so the RGD (wave -1) that
> would regenerate the CRD never lands, and this does **not** self-heal — `retry`/`selfHeal` recompute
> the identical failing comparison. Sync-wave ordering does not save you either, because ArgoCD
> reports `health=None` for a `ResourceGraphDefinition` and treats wave -1 as complete once applied,
> not once KRO has regenerated the CRD. Break it by applying the RGD once out of band, or split the
> change into two commits (schema first, then the instance).

Then label an issue and watch:

```bash
gh issue create --repo <OWNER>/<REPO> --title "..." --body "..."   # create WITHOUT the label
gh issue edit <N> --repo <OWNER>/<REPO> --add-label darkfactory-lambda
```

Create first, label second: the sensor filters on `action: labeled`, and creating an issue with labels
attached emits `opened` (labels already present), which does not match.

```bash
kubectl get wf -n argo -w
kubectl logs -n argo -l workflows.argoproj.io/workflow=df-run-lambda-<N> --prefix --tail=200
```

Healthy provision output ends with `/run -> HTTP 200 {"status":"started"}`.

---

## Part F — Fix rounds (suspend → resume)

The VM is **suspended** after the first PR and resumed for a fix round, so the same warm VM (and its
cloned repo) is reused. Two hard constraints:

- **The window is 8 hours.** `suspendedDurationSeconds=28800` is Lambda's maximum; after that the VM
  **auto-terminates** and the fix round recreates a fresh one (`prior VM not resumable (gone) —
  recreating fresh`). Submit the fix round inside that window if you want to exercise warm resume.
- **The review note must differ from the previous round.** `hook-server.js` keys its `/run` guard on a
  run-id hashed from issue number + note; an identical note hashes to the same id and is correctly
  ignored as a duplicate webhook (`/run duplicate for <id> — ignoring`).

To drive a fix round without a second GitHub identity, submit the workflow directly — the same shape
`iterate.js` builds:

```bash
NOTE_B64=$(gh api repos/<OWNER>/<REPO>/issues/comments/<COMMENT_ID> --jq '.body' | base64 -w0)
# Workflow df-run-lambda-<N>-i<R>, workflowTemplateRef df-run-lambda, parameters:
#   issue-id, issue-number, repo, issue-title, issue-body, base-branch,
#   trigger-label=darkfactory-lambda, iterate-note="", iterate-note-b64=$NOTE_B64
```

Warm resume logs `RESUMING same VM <id> (warm resume)` and keeps the same microvm id and endpoint.

---

## Known issues (observed live, 2026-08)

| Symptom | Status |
|---|---|
| `/run -> HTTP 502` immediately after a successful warm resume; `provision-microvm` exits 1 and the fix round **fails** rather than self-healing | **Open.** Resume itself works (same VM id, same endpoint). The likely cause is that provision waits for infrastructure `state=RUNNING` + an endpoint, which on resume is satisfied almost immediately — before the restored hook-server is listening on `:8080` again. A fresh VM incidentally avoids this because its boot loop takes several seconds. Unconfirmed; a readiness retry on `/run` is the obvious fix. |
| `MicrovmImage` stuck `CREATE_FAILED` never rebuilds after you fix the artifact | **Expected.** `CREATE_FAILED` is terminal on the pre-GA controller: spec patches bump `metadata.generation` but no new build is attempted (`list-microvm-image-builds` shows no records for the new version). Delete the CR and let KRO recreate it. Note `DELETION_POLICY=delete` — the finalizer deletes the real AWS image too. |
| `Bucket.spec.name` change rejected: `Value is immutable once set` | **Expected.** The CRD carries a CEL rule `self == oldSelf`. Delete the `Bucket` CR and let KRO recreate it — *after* the new RGD has synced, or KRO recreates it from the old template. A CR with no ARN and no finalizers issues no `DeleteBucket`, so no S3 data is touched. |
| No runtime logs in CloudWatch (`/aws/lambda/microvms/<image>` has only build streams) | **Known pre-GA limitation** — see benchmark pitfall #3. Use the hook server's `/logs` endpoint (it captures the coder's stdout) rather than CloudWatch. |
| `Microvm` CR shows `RUNNING` while AWS says `SUSPENDED` | **By design.** suspend/resume are imperative SDK ops the ACK controller does not reconcile, which is why the RGD omits the running VM and the shim owns that lifecycle. |

See [`SUBSTRATE-BENCHMARK.md`](SUBSTRATE-BENCHMARK.md) §"Gotchas the Lambda substrate needed" for the
13 substrate quirks already baked into the implementation.
