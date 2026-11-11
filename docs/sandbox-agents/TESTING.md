# Testing the sandbox / isolation work (PR #151)

A closeout script. Part 1 runs anywhere in about five minutes and needs no
cluster. Parts 2–4 need progressively more: a cluster, then Nitro-capable nodes,
then AWS KMS.

**Read this first: what the offline parts can and cannot tell you.** Everything
in Part 1 is a *render-time* property — what manifest the platform produces from
a given Application. That is the right place to catch the failures this feature
is prone to, because the dangerous ones are silent: an isolation field that is
accepted and ignored produces a pod that runs perfectly and isolates nothing.
What Part 1 cannot tell you is whether the kernel boundary actually exists. Only
Part 3 does that, and only on hardware.

---

## Prerequisites

| Tool | Why |
|---|---|
| `helm` ≥ 3.12 | substitutes the chart globals *into* the CUE before KubeVela parses it |
| `vela` ≥ 1.10 | `vela def render`, `vela dry-run --offline` |
| `python3` + `PyYAML` | the assertions parse rendered YAML |
| `kubectl` | Parts 2–4 |
| `aws` CLI | Part 4 |

```bash
pip install pyyaml      # or: brew install python-yaml
```

**One gotcha that will waste your time otherwise.** `vela dry-run --offline`
is not as offline as it sounds: vela builds a client *before* it honours
`--offline`, and exits 1 with an empty stdout when there is no readable
kubeconfig. It never contacts the cluster — any valid kubeconfig will do. All
three scripts below now preflight for this and say so, instead of reporting
guard rows that "pass" because nothing rendered.

```bash
kubectl config current-context    # anything that prints is enough
```

---

## Part 1 — offline, no cluster (~5 min)

```bash
git fetch origin feat/nitro-sandbox-isolation
git checkout feat/nitro-sandbox-isolation
```

### 1.1 Features — does each one emit what its consumer reads?

```bash
bash scripts/test-isolation-features.sh
```

**Expect:** `all feature rows passed` — 33 rows, exit 0.

This asserts *content*, not acceptance: the four env vars a session-isolated
agent's image reads in order to claim a sandbox, the RBAC without which those
env vars are decoration, the runtime class per trust model, the annotations the
Nitro runtime reads, and the card an operator reads back. Spot-check a few:

| Row | What it pins down |
|---|---|
| F1 / F6 | `sandbox: true` → `isolated-sandbox`; `sandboxMode: confidential` → `confidential-sandbox` |
| F8 / H14 | confidential pods set `enableServiceLinks: false` — a **generator requirement**, see 1.3 |
| F10 | the *same* Application on a Kata cluster selects `kata-clh` — one annotation, no manifest change |
| G1–G4 | `AGENT_ISOLATION`, `SANDBOX_WARM_POOL`, `SANDBOX_NAMESPACE`, `AGENT_SESSION_KEY` |
| G5–G7 | the claimer `Role` can `create` sandboxclaims, can only *read* sandboxes, and is bound to the agent's own SA |
| H9–H10 | `networkIsolated` reaches both the template annotation and the card |

### 1.2 Guards — does every forbidden combination still fail?

```bash
bash scripts/test-isolation-matrix.sh        # add -v to see each rejection
```

**Expect:** `failed rows: 0` — 52 PASS, 4 SKIP, exit 0.

The 4 SKIPs are examples needing an `appmod-blueprints` trait that is not in this
repo; they are reported as SKIP rather than FAIL on purpose.

This is the half that matters most over time. A guard that quietly stops firing
is worse than one that never existed, because the thing it protected still looks
fine. Each `C` row now asserts **the guard's own name**, so a row cannot pass on
an unrelated rejection.

**Prove the matrix can actually fail**, which is worth doing once — a green suite
you have never seen go red is not evidence:

```bash
# delete a guard in a scratch copy and watch the matching row flip
cp -R . /tmp/matrix-probe && cd /tmp/matrix-probe
python3 - <<'PY'
import pathlib, re
p = pathlib.Path('platform/oam/definitions/components/agent.cue')
t = p.read_text()
i = t.index('_sandboxModeNeedsSandbox')
start = t.rindex('\tif ', 0, i)
end = t.index('\n\t}\n', i) + 4
p.write_text(t[:start] + t[end:])
PY
vela def render platform/oam/definitions/components/agent.cue \
  -o gitops/addons/charts/oam-agent-components/templates/agent.yaml \
  --message "# Code generated from CUE definitions. DO NOT EDIT."
bash scripts/test-isolation-matrix.sh | grep C12
cd - && rm -rf /tmp/matrix-probe
```

The row flips, and *how* it flips is the point:

```
FAIL  C12  agent: sandboxMode without sandbox:true
           expected=_sandboxModeNeedsSandbox actual=reject:_cedarPolicyNeedsSandbox
```

The Application was still rejected — by the *sibling* guard, because that fixture
also sets `cedarPolicy`. A row asserting only `reject` would have **passed here**
with the guard under test deleted. That is finding 6 in one line, and the reason
these rows now assert the guard's name.

Also worth seeing once: a **broken definition set must fail fast**, not run every
row against nothing. Append a syntax error to any `.cue` and re-run — you should
get `cannot build definition set 'nitro'`, exit 1, and **zero rows run**.

### 1.3 The Cedar policy generator

The real binary is a signature-verified Linux ELF, so this is the one place a
stub is reasonable for a quick check — but the stub must reproduce the
generator's actual refusal, which is what the original work missed:

```bash
T=$(mktemp -d); mkdir -p "$T/bin"
cat > "$T/bin/genpolicy" <<'EOF'
#!/bin/bash
grep -q enableServiceLinks pod.yaml || {
  echo "manifest does not set enableServiceLinks: false" >&2; exit 1; }
echo "ZGVhZGJlZWY$(printf 'A%.0s' {1..140})"
echo "PCR17: $(printf 'b%.0s' {1..96})"
EOF
chmod +x "$T/bin/genpolicy"

./scripts/nitro/gen-cedar-policy.sh \
  --app platform/oam/examples/example-agent-sandbox-confidential.yaml \
  --component regulated-assistant --profile confidential \
  --genpolicy "$T/bin/genpolicy" --out "$T/policy.b64"
```

**Expect**, and each line is load-bearing:

```
Rollout/regulated-assistant
==> serviceAccountName=regulated-assistant -- adding --with-pod-identity (--no-pod-identity to skip)
==> confidential profile -- adding --deny-exec --deny-logs (--allow-exec-logs to keep them)
policy written: .../policy.b64  (151 base64 chars)
```

- the **stub did not refuse**, so the rendered confidential pod carries
  `enableServiceLinks: false`. Before this PR's fix it refused every rendered
  agent and sandbox pod;
- `--with-pod-identity` was added from the pod's `serviceAccountName`. Pod
  Identity injects `AWS_CONTAINER_CREDENTIALS_FULL_URI`, a token-file variable
  and a projected volume, all of which a confidential policy pins — so a policy
  generated without it **denies the pod** over an env mismatch;
- `--deny-exec --deny-logs` were added, because neither is blocked by the policy
  unless asked for and both are how you read a confidential pod from outside it.

Then the negative, which proves the hint rather than the happy path — point the
confidential profile at an *isolated* agent:

```bash
cat > "$T/iso.yaml" <<'YAML'
apiVersion: core.oam.dev/v1beta1
kind: Application
metadata: {name: iso, namespace: default}
spec:
  components:
    - name: iso-agent
      type: agent
      properties:
        description: "d"
        systemMessage: "s"
        sandbox: true
YAML
./scripts/nitro/gen-cedar-policy.sh --app "$T/iso.yaml" --component iso-agent \
  --profile confidential --genpolicy "$T/bin/genpolicy"
```

**Expect** `genpolicy failed (exit 1)` followed by the explanation naming
`sandboxMode: confidential` as the fix, and saying that `--allow-additional-env`
gets past it but stops pinning the environment — most of what the profile is for.

Finally, the portability check. `"${EXTRA[@]}"` with an empty array under
`set -u` aborts on bash 3.2, which is the `/bin/bash` every macOS ships, and the
empty case is the common one:

```bash
/bin/bash --version | head -1                        # expect 3.2.x on macOS
/bin/bash ./scripts/nitro/gen-cedar-policy.sh --help # must print usage, not abort
rm -rf "$T"
```

### 1.4 The `agent-sandbox` chart

This is where a live cluster regressed: the chart default had flipped to Nitro
while the registry passed no annotation, so Kata clusters had their warm pool
recreated on a class that may not exist.

```bash
C=gitops/addons/charts/agent-sandbox
IMG=--set=coderTemplate.image=example.com/coder:1

# the default MUST still be Kata
helm template s $C $IMG | grep 'runtimeClassName:'          # kata-clh

# and byte-identical to main, which is the actual claim. Needs a second
# worktree, because the comparison is between two checkouts of the chart:
git worktree add /tmp/oap-main origin/main -f >/dev/null 2>&1
diff <(helm template s $C $IMG) \
     <(helm template s /tmp/oap-main/$C $IMG) && echo "BYTE-IDENTICAL to main"
git worktree remove /tmp/oap-main --force

helm template s $C $IMG --set runtime=nitro | grep 'runtimeClassName:'   # isolated-sandbox

# a typo must FAIL, not silently give you a Kata pool
helm template s $C $IMG --set runtime=nitr0 2>&1 | tail -2

# Nitro-only values on a Kata pool must FAIL rather than be dropped
helm template s $C $IMG --set runtime=kata --set nitro.networkIsolated=true 2>&1 | tail -2
helm template s $C $IMG --set runtime=kata --set nitro.cedarPolicy=abc 2>&1 | tail -2
```

Helm prints `Use --debug flag to render out invalid YAML` on failure and puts the
real message behind `--debug`; add it to read the guard text.

### 1.5 Generated definitions are in step with their CUE

A stale committed template is *deployable* while the CUE says something else.
That drift exists on `main` today — see NITRO-DESIGN §7.1 — and this is the check
that makes it visible:

```bash
for f in platform/oam/definitions/components/*.cue; do
  b=$(basename "$f" .cue)
  out=gitops/addons/charts/oam-agent-components/templates/$b.yaml
  [ -f "$out" ] || continue
  vela def render "$f" -o "/tmp/chk-$b.yaml" \
    --message "# Code generated from CUE definitions. DO NOT EDIT."
  diff -q "/tmp/chk-$b.yaml" "$out" >/dev/null && echo "  OK    $b" || echo "  DRIFT $b"
done
```

**Expect** `OK` for all eight. CI (`.github/workflows/isolation-matrix.yml`) runs
this, the matrix and the chart assertions on every PR touching these paths.

---

## Part 2 — on a cluster, no Nitro nodes needed (hub)

Admission is a different authority from `vela dry-run`, and these are the checks
only it can answer.

```bash
kubectl apply --dry-run=server \
  -f gitops/addons/charts/oam-agent-components/templates/agent.yaml \
  -f gitops/addons/charts/oam-agent-components/templates/sandbox.yaml
```

### 2.1 Unknown properties — a real gap in the offline tooling

I found that `vela dry-run --offline` **accepts a property that does not exist**:

```bash
# offline: rc=0, silently ignored
cat > /tmp/bogus.yaml <<'YAML'
apiVersion: core.oam.dev/v1beta1
kind: Application
metadata: {name: bogus, namespace: default}
spec:
  components:
    - name: p
      type: sandbox
      properties:
        description: "d"
        image: public.ecr.aws/docker/library/python:3.12-slim
        thisFieldDoesNotExist: true
YAML
```

KubeVela's admission webhook validates `properties` against the
ComponentDefinition's generated OpenAPI schema, so on a real cluster this
*should* be rejected. **Please confirm that** — it is the one thing I could not:

```bash
kubectl apply --dry-run=server -f /tmp/bogus.yaml
```

If it is accepted, then a typo'd isolation field (`sandboxmode`, `networkIsolate`)
is silently ignored cluster-side too, which is the same class of failure as
review finding 2 and worth its own issue.

### 2.2 The guards, against the real webhook

Each of these must be **rejected**, with a message naming the remedy:

```bash
# sandboxMode without sandbox: true
kubectl apply --dry-run=server -f - <<'YAML'
apiVersion: core.oam.dev/v1beta1
kind: Application
metadata: {name: t1, namespace: default}
spec:
  components:
    - name: t1-agent
      type: agent
      properties:
        description: "d"
        systemMessage: "s"
        sandboxMode: confidential
        cedarPolicy: "cGVybWl0KHByaW5jaXBhbCwgYWN0aW9uLCByZXNvdXJjZSk7IC8vIHBhZGRpbmcgdG8gcGFzcyB0aGUgc2hhcGUgY2hlY2s="
YAML

# an unreplaced example placeholder
kubectl apply --dry-run=server -f - <<'YAML'
apiVersion: core.oam.dev/v1beta1
kind: Application
metadata: {name: t2, namespace: default}
spec:
  components:
    - name: t2-agent
      type: agent
      properties:
        description: "d"
        systemMessage: "s"
        sandbox: true
        cedarPolicy: "REPLACE_ME_WITH_BASE64_CEDAR_POLICY"
YAML
```

### 2.3 The Kata path, which is what regressed

On a cluster with the Kata node layer, set the annotation on the cluster secret
and confirm no Application changes:

```
sandbox_runtime_class: kata-clh
confidential_sandbox_runtime_class:        # left EMPTY: Kata cannot attest
sandbox_runtime: kata                      # the agent-sandbox warm pool
```

Then:

```bash
kubectl get sandboxtemplate -A -o custom-columns=\
'NAME:.metadata.name,CLASS:.spec.podTemplate.spec.runtimeClassName'
kubectl get sandboxwarmpool -A
kubectl get pods -l agent-sandbox.io/role=coder -o wide   # must be Running, not Pending
```

**The regression to watch for:** warm-pool pods `Pending` with no node matching
`eks.amazonaws.com/nitro-sandbox`. That is the finding-1 symptom, and it is what
the Kata default restores.

### 2.4 Confidential must fail closed on Kata

With `confidential_sandbox_runtime_class` empty, `sandboxMode: confidential` has
to be **rejected** naming the annotation — not quietly handed a `kata-clh` pod
that cannot attest anything. Then the sharper case: point
`confidential_sandbox_runtime_class` at `kata-clh` deliberately. It must *still*
be rejected, by `_confidentialNeedsNitro`, because Kata has no confidential
equivalent.

---

## Part 3 — on Nitro-capable nodes (k8s 1.36+, US commercial region)

Nothing here was verified during this work — there was no Nitro node available —
so treat all of it as unproven until you run it.

```bash
kubectl get runtimeclass isolated-sandbox confidential-sandbox
kubectl apply -f examples/nitro-sandbox/pod-hello-sandbox.yaml
kubectl wait --for=condition=Ready pod/hello-sandbox --timeout=300s
```

### 3.1 The decisive check: does the pod have its own kernel?

A container *cannot* have a different kernel from its node. So a pod reporting a
different kernel version is, necessarily, in its own VM. Everything else —
labels, class names, annotations — is a claim; the kernel is a consequence.

```bash
bash scripts/nitro/verify-isolation.sh hello-sandbox -n default
```

**Expect** `pod kernel differs from node kernel -- this is a real microVM`.

If your workload image is distroless or scratch there is no shell to exec, and
the script now reports that as *skipped* rather than a FAIL (it used to exit 1,
which was simply wrong). To read the kernel anyway:

```bash
bash scripts/nitro/verify-isolation.sh <pod> --debug-image busybox
```

That uses an ephemeral container, which joins the same sandbox — so its kernel
*is* the guest's. Note it permanently alters the pod's spec; the pod must be
recreated to remove it, which is why it is opt-in.

### 3.2 Per-session isolation, end to end

```bash
kubectl apply -f platform/oam/examples/example-agent-session-isolation.yaml
kubectl get sandboxwarmpool,sandboxtemplate -n default
kubectl get configmap <pool>-sandbox -o jsonpath='{.data}' | python3 -m json.tool
```

Then drive two different users through the agent and confirm two distinct
`SandboxClaim`s, one sandbox each, and that they are destroyed on idle
(`idleTimeout`, default 15m).

**Known limitation, stated plainly:** `sessionIsolationEnabled` is a declarative
switch. It asserts the cluster has the `agent_sandbox` addon *and* an agent image
that claims a sandbox per session. Nothing verifies the second half — an agent on
an image that never calls `SandboxClaim` will render, deploy, serve traffic and
share one process between every user. If you see one sandbox for two users, check
the image before the platform.

### 3.3 The real generator

```bash
bash scripts/nitro/download-runtime-binaries.sh     # signature-verified
./scripts/nitro/gen-cedar-policy.sh --from pod/attested-demo -n default \
  --profile confidential --out from-live.b64
```

`--from pod/<name>` is the only genuinely post-webhook source: mutating webhooks
run on the **Pod**, not on the template that produced it, so Pod Identity's
injected env and token volume appear there and nowhere else. Generate from
`--app` in CI, regenerate from the live pod, and **diff the two** — a difference
is your admission chain mutating the pod, and in confidential mode that is the
gap between a pod that starts and a pod that is denied.

---

## Part 4 — attested secret release (AWS KMS, billable)

`examples/nitro-sandbox/README.md` is the walkthrough. Two things to insist on:

**The gate must reject the wrong guest.** Change one byte of the Cedar policy,
leave the old PCR17 in the key policy, and confirm
`kms:Decrypt: AccessDeniedException` on the image pull. Nothing else in the stack
produces that error.

**The gate must also reject *you*.** This is the control the example was missing,
and the one that catches a key policy granting `kms:*` to the account root — which
refuses nobody while looking identical from outside:

```bash
aws kms decrypt --key-id <key-arn> --ciphertext-blob fileb:///tmp/anything.bin
```

Read the **error**, not the failure:

| Result | Verdict |
|---|---|
| `AccessDeniedException` | **PASS** — the Deny statement is doing its job |
| `InvalidCiphertextException` | **FAIL** — authorization *succeeded*; KMS got as far as your bytes |
| plaintext returned | **FAIL**, obviously |

> A KMS key is billable and cannot be deleted immediately — deletion schedules a
> 7–30 day waiting period. `create-attested-key.sh` requires `--yes`, refuses to
> repoint an existing alias, and now refuses to create a key at all when it
> cannot determine whether the alias exists.

---

## Rough edges I hit, neither of them blocking

1. **`type: sandbox` requires `description`, and omitting it fails badly.** You
   get `Failed to have the workload/trait unstructured` with raw CUE dumped and
   `description: string` buried in it, rather than "description is required".
   Every shipped example sets it, so this only bites someone writing a new pool.
2. **`vela dry-run --offline` accepts unknown properties** (§2.1). Confirming
   whether admission catches them is the one open item I'd most like checked.

## Closeout checklist

| | Check | Where |
|---|---|---|
| ☐ | 33 feature rows pass | 1.1 |
| ☐ | 52 matrix rows pass, 0 fail | 1.2 |
| ☐ | a deleted guard flips its row to FAIL | 1.2 |
| ☐ | generator accepts the confidential render; hint fires on an isolated one | 1.3 |
| ☐ | chart default is Kata; typo and Nitro-on-Kata both fail | 1.4 |
| ☐ | no CUE/YAML drift | 1.5 |
| ☐ | both definitions pass `--dry-run=server` | 2 |
| ☐ | **unknown properties rejected by admission?** | 2.1 |
| ☐ | guards reject against the real webhook | 2.2 |
| ☐ | Kata warm pool Running, not Pending | 2.3 |
| ☐ | confidential fails closed on Kata, both ways | 2.4 |
| ☐ | pod kernel ≠ node kernel | 3.1 |
| ☐ | two users → two sandboxes | 3.2 |
| ☐ | `--app` and `--from pod/` policies diffed | 3.3 |
| ☐ | wrong guest denied | 4 |
| ☐ | **administrator denied** | 4 |
