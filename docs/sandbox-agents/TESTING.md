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

# the default is NITRO -- and must carry no Kata node scheduling, which is the
# bug that made every warm pod Pending (it would demand both node labels)
# Check the SandboxTemplate's POD SPEC, not the whole render -- the chart also
# emits the three Kata RuntimeClass objects, which legitimately carry
# katacontainers.io/kata-runtime in their own scheduling block.
helm template s $C $IMG | python3 -c '
import sys, yaml
for d in yaml.safe_load_all(sys.stdin):
    if d and d.get("kind") == "SandboxTemplate":
        s = d["spec"]["podTemplate"]["spec"]
        print("class       :", s.get("runtimeClassName"))
        print("nodeSelector:", s.get("nodeSelector", "absent"))
        print("tolerations :", s.get("tolerations", "absent"))'
# class: isolated-sandbox, nodeSelector: absent, tolerations: absent

# kata is one value away, and keeps its nodeSelector + toleration
helm template s $C $IMG --set runtime=kata | grep 'runtimeClassName:'  # kata-clh

# NOT byte-identical to main any more, and that is the intended change: the
# default moved from kata-clh to isolated-sandbox once Nitro was validated on
# real hardware. The diff below should show exactly that and nothing else --
# the runtime class, and the Kata nodeSelector/toleration disappearing with it.
git worktree add /tmp/oap-main origin/main -f >/dev/null 2>&1
diff <(helm template s /tmp/oap-main/$C $IMG) <(helm template s $C $IMG)
git worktree remove /tmp/oap-main --force

# A cluster whose nodes ARE Kata pins the runtime instead of inheriting it, and
# that render IS byte-identical to main:
git worktree add /tmp/oap-main origin/main -f >/dev/null 2>&1
diff <(helm template s /tmp/oap-main/$C $IMG) \
     <(helm template s $C $IMG --set runtime=kata) && echo "kata path UNCHANGED"
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
# BOTH pools, because this annotation set touches both and they use
# different role labels: the agent-sandbox chart's Dark Factory pool is
# role=coder, an OAM `type: sandbox` pool is role=session.
kubectl get pods -l agent-sandbox.io/role=coder   -o wide   # chart pool
kubectl get pods -l agent-sandbox.io/role=session -o wide   # OAM pools
# both must be Running, not Pending
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

## Part 3 — on Nitro-capable nodes: every example, step by step

**This is the only part that tests isolation rather than manifests.** Parts 1–2
prove the platform emits the right pod spec and rejects the wrong Application.
Nothing in them can tell you a kernel boundary exists. That is what this part is
for.

### What has actually been run, and what has not

Executed on the **`hub`** cluster (EKS Auto Mode, k8s v1.37.1, us-west-2):

| Step | Status |
|---|---|
| **3.0** cluster gate + smoke test | **PASSED.** Auto Mode provisioned a `c8a.large` sandbox node on demand (~2 min); pod kernel **6.18.54** vs node **6.18.56**; DMI `c8a.enclave`; virtio disks `vda vdb vdc`. `verify-isolation.sh` → **7 PASS / 0 FAIL**, exit 0 |
| confidential runtime class, raw pod, no policy | **PASSED.** Refused by the runtime — see the box in 3.3 |
| **confidential mode WITH a real Cedar policy** | **PASSED END TO END.** Real `eks-nitro-sandbox-genpolicy`, pod Running 1/1, `exec` denied by the policy. Procedure in 3.3a |
| `download-runtime-binaries.sh` | **PASSED.** Signature `Verified OK`, all 8 files `OK`, on both `aarch64` and `x86_64` |
| `verify-isolation.sh` on a confidential pod | **PASSED.** 5 PASS / 0 FAIL, correctly reports exec-denial as the guarantee working |
| **3.1–3.5** (the OAM examples) | **BLOCKED on `hub` today.** Reasons below — these are cluster-state problems, not problems with the examples |
| **3.6** attested pod, **Part 4** KMS | **Not run.** Needs an encrypted image and a billable KMS key |

Three things block 3.1–3.5 on `hub` right now, all of which you fix before
testing rather than by changing the PR:

1. **The deployed `agent` ComponentDefinition is from `main`.** It has `sandbox`
   but not `sandboxMode`, `cedarPolicy`, `isolation` or `sandboxTemplate`, so
   every example in 3.3–3.5 is rejected on an unknown field. Sync the
   `oam-agent-components-hub` ArgoCD app to this branch first.
2. **`sandbox: true` is broken on `hub` independently of this PR.** The deployed
   definition contains the Helm placeholder **unsubstituted**:

   ```
   _sandboxRuntimeClass: "{{ .Values.global.sandboxRuntimeClass }}"
   ```

   So a `sandbox: true` agent renders `runtimeClassName:
   {{ .Values.global.sandboxRuntimeClass }}`, which the API server rejects. No
   live agent on `hub` sets `sandbox: true`, so nobody is hitting it — but the
   feature cannot work there until the chart is rendered through Helm properly.
   `verify-isolation.sh` has a dedicated check for exactly this
   ("runtimeClassName contains an unsubstituted Helm placeholder"), which is how
   it was spotted.
3. **The `agent_sandbox` addon is not installed** — no `sandboxclaims`,
   `sandboxes`, `sandboxtemplates` or `sandboxwarmpools` CRDs — so `type:
   sandbox`, warm pools and claims (3.2, 3.4, 3.5) cannot be created at all.

Everything below is written to be run in order. Where a step has been verified,
the real observed output is shown rather than an idealised one.

Requirements: Kubernetes **1.36+**, a **US commercial region**, and either EKS
Auto Mode (which provisions a sandbox-capable node on demand) or your own nodes
from a Nitro Sandbox AMI launched with `EnclaveOptions.Enabled=true`.

### 3.0 Gate: can this cluster run a sandbox at all?

Do this before touching any example. If it fails, every step below fails for the
same reason and the errors will be much harder to read.

```bash
kubectl get runtimeclass isolated-sandbox confidential-sandbox
```

```
NAME                   HANDLER                AGE
confidential-sandbox   confidential-sandbox   5m
isolated-sandbox       isolated-sandbox       5m
```

Then the smoke test, which is a raw Pod on purpose — no OAM, no KubeVela, no
chart, so a failure here is the cluster and nothing of ours:

```bash
kubectl apply -f examples/nitro-sandbox/pod-hello-sandbox.yaml
kubectl wait --for=condition=Ready pod/hello-sandbox --timeout=300s
kubectl logs hello-sandbox
```

```
/sys/class/dmi/id/product_name:  c8a.enclave
disks:   dm-0 vda vdb vdc
kernel:  6.18.54
```

That is the real output from `hub`. Compare it with the node:

```console
$ kubectl get node <node> -o jsonpath='{.status.nodeInfo.kernelVersion}'
6.18.56
```

**6.18.54 inside, 6.18.56 outside.** Two different kernels on one machine, which
a container cannot do.

On Auto Mode the node is provisioned on demand, so expect the pod to sit
`Pending` for a minute or two first, with a `FailedScheduling` event naming
`eks.amazonaws.com/nitro-sandbox In [true]` — that is the RuntimeClass admission
controller having force-merged the node selector, working as intended. The node
that appears is sandbox-capable and advertises slots:

```console
$ kubectl get nodes -l eks.amazonaws.com/nitro-sandbox=true
$ kubectl get node <node> -o jsonpath='{.status.allocatable.eks\.amazonaws\.com/nitro-sandbox}'
10
```

Note the instance type will change under you: `c6a.large`/`c6g.large` nodes are
**not** sandbox-capable (2 vCPU is below the enclave minimum, and Graviton is a
separate question), and Auto Mode picked `c8a.large` instead.

Three independent signals, and the third is the one that matters: a container
*cannot* have a different kernel from its node. The `.enclave` suffix and the
virtio disks (`vda`, not `nvme`) corroborate it.

**If it stays Pending**, no node can take it. Read the events —
`kubectl describe pod hello-sandbox | grep -A5 Events` — and check the usual
causes: a cluster below 1.36, a NodeClass using FIPS AMIs, or a
GPU/Neuron/multi-NUMA instance type, none of which Auto Mode provisions as a
sandbox node. This Pending-rather-than-running is the designed behaviour, not a
bug: the alternative is a pod that runs with no isolation and reports success.

```bash
kubectl delete -f examples/nitro-sandbox/pod-hello-sandbox.yaml
```

### Before you apply any OAM example: two things that will bite

**1. Two examples used to collide, and your namespace choice still matters.**
`example-sandbox-confidential.yaml` declared a sandbox component named
`analyst-session` — the same name `example-agent-session-isolation.yaml` uses —
both in `default`. Applied together, the two Applications each believed they
owned one warm pool, both reconciled, neither errored, and whichever synced last
won. That is fixed (it is now `regulated-session`, and matrix row D5 checks the
shipped examples against each other), but the general rule stands: **apply one
example at a time, or put each in its own namespace**, and clean up between.

**2. The two confidential examples carry a placeholder `cedarPolicy`.** It is
correctly *shaped*, so the Application renders and admission accepts it — and it
base64-decodes to the instruction for replacing it:

```bash
grep -h cedarPolicy platform/oam/examples/example-*-confidential.yaml \
  | sed 's/.*: "//;s/"//' | head -1 | base64 -d; echo
# REPLACE THIS. Generate the real policy with scripts/nitro/gen-cedar-policy.sh ...
```

Deploy it unchanged and the pod does **not** start: the sandbox agent rejects the
policy. That is the intended behaviour, and it is worth seeing once (3.3).

---

### 3.1 `example-agent-sandbox.yaml` — an isolated agent

The baseline: one boolean on an ordinary agent. Everything else about the agent —
gateway registration, MCP wiring, two replicas, blue-green — is unchanged.

| | |
|---|---|
| Application | `my-assistant-sandboxed` |
| Component | `oap-assistant-sbx` (`type: agent`) |
| Asks for | `sandbox: true`, `sandboxMode: isolated` |
| Proves | the trust model where the node is trusted and the workload is not |

```bash
kubectl apply -f platform/oam/examples/example-agent-sandbox.yaml
kubectl get application my-assistant-sandboxed -w        # until status: running
```

Confirm the class reached the pods, then confirm the boundary:

```bash
kubectl get pods -l app.kubernetes.io/name=oap-assistant-sbx \
  -o custom-columns='NAME:.metadata.name,CLASS:.spec.runtimeClassName,NODE:.spec.nodeName,STATUS:.status.phase'
```

Expect **two** pods (`replicas: 2`), each `isolated-sandbox`, each Running.

```bash
bash scripts/nitro/verify-isolation.sh -l app.kubernetes.io/name=oap-assistant-sbx
```

Expect `pod kernel differs from node kernel -- this is a real microVM`.

Two pods on the **same node** is the sharper version of this check — it means one
node is running two independent kernels, which is the whole claim:

```bash
kubectl get pods -l app.kubernetes.io/name=oap-assistant-sbx \
  -o jsonpath='{range .items[*]}{.spec.nodeName}{"\t"}{.metadata.name}{"\n"}{end}'
# if two share a node:
for p in <pod-a> <pod-b>; do kubectl exec "$p" -- uname -r; done
kubectl get node <node> -o jsonpath='{.status.nodeInfo.kernelVersion}'; echo
```

Then confirm the agent still *works* — isolation that breaks the agent is not a
feature. It registers with the gateway and has the `mcp-time` server wired:

```bash
kubectl get configmap oap-assistant-sbx-card -o jsonpath='{.data}' | python3 -m json.tool
kubectl get httproute oap-assistant-sbx
# drive a prompt through the gateway and confirm the time tool is called
```

```bash
kubectl delete -f platform/oam/examples/example-agent-sandbox.yaml
```

### 3.2 `example-sandbox-isolated.yaml` — a standalone warm pool

No agent involved. This is the pool on its own, which is what an MCP server or
the Dark Factory coder flow claims from.

| | |
|---|---|
| Application | `session-code-sandbox` |
| Component | `code-session` (`type: sandbox`) |
| Asks for | `mode: isolated`, `warmPool: 2`, `service: true` |
| Proves | warm pods exist *before* anyone asks, and a claim is fast |

```bash
kubectl apply -f platform/oam/examples/example-sandbox-isolated.yaml
kubectl get sandboxwarmpool code-session \
  -o custom-columns='NAME:.metadata.name,REPLICAS:.spec.replicas,TEMPLATE:.spec.sandboxTemplateRef.name'
kubectl get sandboxtemplate code-session-template \
  -o jsonpath='{.spec.podTemplate.spec.runtimeClassName}'; echo    # isolated-sandbox
```

The point of a warm pool is that the pods are **already there**:

```bash
kubectl get pods -l agent-sandbox.io/role=session -o wide     # expect 2, Running
```

> **Mind the label.** An OAM `type: sandbox` pool labels its pods
> `agent-sandbox.io/role=session`. The `agent-sandbox` *chart* — the Dark Factory
> coder pool — uses `role=coder`. Both can exist on one cluster, so selecting on
> the wrong one returns nothing, or somebody else's pods.

Read the card, which is how an operator sees what they actually got rather than
what the Application asked for:

```bash
kubectl get configmap code-session-sandbox -o jsonpath='{.data}' | python3 -m json.tool
```

```json
{
  "mode": "isolated",
  "runtimeClass": "isolated-sandbox",
  "warmReplicas": "2",
  "idleTimeout": "15m",
  "maxLifetime": "4h",
  "networkIsolated": "false",
  "service": "true"
}
```

Now claim one and time it — this is the number that justifies a warm pool:

```bash
cat <<'YAML' | kubectl apply -f -
apiVersion: extensions.agents.x-k8s.io/v1beta1
kind: SandboxClaim
metadata:
  # The claim NAME is the idempotency key: the same session re-claiming finds
  # its own instance rather than a second one, which is what makes a retry safe.
  # Name it after the thing being isolated.
  name: probe-session-7f3a9c
  namespace: default
  labels:
    app.kubernetes.io/name: code-session
spec:
  # The POOL, not the template. warmPoolRef is the claim's only required field,
  # which is why the sandbox component renders a pool even at warmPool: 0.
  warmPoolRef:
    name: code-session
YAML
time kubectl wait --for=jsonpath='{.status.phase}'=Bound sandboxclaim/probe-session-7f3a9c --timeout=120s
kubectl get sandboxclaim probe-session-7f3a9c -o yaml | grep -A5 status:
```

A warm claim should bind in **seconds**. If it takes as long as a cold pod start,
the pool is not actually warm — check that the warm pods were Running *before*
you claimed.

Confirm the claimed sandbox is a real microVM, and that the pool refilled:

```bash
SBX=$(kubectl get sandboxclaim probe-session-7f3a9c -o jsonpath='{.status.sandboxRef.name}')
kubectl get sandbox "$SBX" -o jsonpath='{.status.podRef.name}'; echo
bash scripts/nitro/verify-isolation.sh <that-pod>
kubectl get pods -l agent-sandbox.io/role=session            # back to 2
```

Then idle it out — `idleTimeout: 15m` — and confirm the sandbox is destroyed
rather than lingering:

```bash
kubectl delete sandboxclaim probe-session-7f3a9c
kubectl get sandbox,pods -l agent-sandbox.io/role=session
kubectl delete -f platform/oam/examples/example-sandbox-isolated.yaml
```

### 3.3 `example-agent-sandbox-confidential.yaml` — a confidential agent

The trust model where the **node operator is untrusted too**. This is the one
worth doing carefully, because it has a step order and a negative control.

| | |
|---|---|
| Application | `regulated-assistant` |
| Component | `regulated-assistant` (`type: agent`) |
| Asks for | `sandboxMode: confidential` + `cedarPolicy`, image pinned by digest |
| Proves | exec/stdin denied from the host, and the policy is enforced not advisory |

**Step 1 — see it refuse the placeholder.** Do this first; it takes one minute
and it is the only way to know the policy is load-bearing:

```bash
kubectl apply -f platform/oam/examples/example-agent-sandbox-confidential.yaml
kubectl get pods -l app.kubernetes.io/name=regulated-assistant
kubectl describe pod -l app.kubernetes.io/name=regulated-assistant | grep -A10 Events
```

Expect the pod **not** to start. A pod that starts here means the policy is not
being enforced, and that is a finding — stop and report it.

> **This behaviour CHANGED during the preview, so check which one you get.**
> Measured 2026-08-20 on a private-preview cluster (m7i.xlarge): an unpolicied
> confidential pod **ran**, and `kubectl exec … -- id` returned `uid=0(root)` —
> confidential in name only, with exec denial being policy-driven rather than
> automatic.
>
> Measured 2026-11-11 on `hub` (Auto Mode, v1.37.1, c8a.large): the same pod
> shape is refused outright —
>
> ```
> FailedCreatePodSandBox  ...  CreateSandbox: resolve sandbox policy:
>   confidential-sandbox requires the sandbox.amazonaws.com/policy annotation;
>   a policy is required in confidential mode (default-deny is enforced)
> ```
>
> — and loops in `ContainerCreating` indefinitely, retrying every ~15s, with
> the reason only in kubelet events.
>
> **So if your pod STARTS here, you are on the older behaviour and the test is
> still meaningful** — go on to step 5 and confirm `exec` is allowed, which is
> the "confidential in name only" failure. If it loops in
> `ContainerCreating` with the message above, you are on default-deny.
>
> Either way `cedarPolicy` is required, for opposite reasons: on the old AMI it
> is the only thing preventing an unprotected pod; on the new one it is what
> lets the pod start at all. Nothing in a manifest tells you which AMI your
> nodes run, which is why the guard is unconditional. See NITRO-DESIGN §4.

```bash
kubectl delete -f platform/oam/examples/example-agent-sandbox-confidential.yaml
```

**Step 2 — get the real binaries.**

```bash
bash scripts/nitro/download-runtime-binaries.sh
```

Expect `Verified OK` and all files `OK` against the signed manifest. This is a
signature check, not a checksum of a file you just downloaded from the same
place — if it fails, do not proceed.

**Step 3 — re-resolve the image digest.** The example pins one, and a pinned
digest in a repo goes stale. Confidential mode pins the image *in the policy*, so
a mismatch is a denied pod:

```bash
docker buildx imagetools inspect public.ecr.aws/z0a4o2j5/strands-agent:v1.6.0 \
  | grep Digest
# put that value in the example's `image:` field if it differs
```

**Step 4 — generate the policy from the rendered pod spec.** Not from the
Application: what runs is a pod template rendered from CUE by KubeVela and
wrapped in an Argo Rollout, and the policy pins *that*.

```bash
./scripts/nitro/gen-cedar-policy.sh \
  --app platform/oam/examples/example-agent-sandbox-confidential.yaml \
  --component regulated-assistant \
  --profile confidential
```

Expect, and read each line:

```
Rollout/regulated-assistant
==> serviceAccountName=regulated-assistant -- adding --with-pod-identity (--no-pod-identity to skip)
==> confidential profile -- adding --deny-exec --deny-logs (--allow-exec-logs to keep them)
policy written: regulated-assistant-cedar-policy.b64  (N base64 chars)
```

plus the predicted **PCR17**. Both auto-added flags matter: without
`--with-pod-identity` the policy denies the pod over the env Pod Identity
injects, and `--deny-exec --deny-logs` is what actually closes the two ways to
read a confidential pod from outside it.

Paste the base64 into the example's `cedarPolicy` and apply:

```bash
kubectl apply -f platform/oam/examples/example-agent-sandbox-confidential.yaml
kubectl get pods -l app.kubernetes.io/name=regulated-assistant \
  -o custom-columns='NAME:.metadata.name,CLASS:.spec.runtimeClassName,STATUS:.status.phase'
```

Expect `confidential-sandbox` and Running. **The pod starting is the test
passing** — the policy was accepted and measured.

**Step 5 — confirm the confidential guarantee, which is exec being denied.**

```bash
kubectl exec -it -l app.kubernetes.io/name=regulated-assistant -- sh
```

This must **fail**. In isolated mode it succeeds; here the Cedar policy refuses
it so the parent instance cannot introspect the guest. `kubectl logs` should
likewise be refused, because of `--deny-logs`.

```bash
bash scripts/nitro/verify-isolation.sh -l app.kubernetes.io/name=regulated-assistant
```

The script knows this case and reports
`exec is DENIED, which is the confidential guarantee working` rather than a
scary FAIL — and tells you that verifying a confidential sandbox means checking
the **attestation** instead, which is Part 4.

**Step 6 — the policy is a pin, so prove it pins.** Change anything the policy
covers — bump `replicas`, add an env var, change the digest — without
regenerating:

```bash
kubectl patch application regulated-assistant --type=merge \
  -p '{"spec":{"components":[{"name":"regulated-assistant","type":"agent","properties":{"replicas":2}}]}}'
kubectl describe pod -l app.kubernetes.io/name=regulated-assistant | grep -A10 Events
```

The new pod must be **denied**. This is the operational cost of confidential mode
and the thing to internalise: **the policy and the pod spec are one artifact, and
they are regenerated together, in one commit.** A new image digest alone is
enough to require it.

```bash
kubectl delete -f platform/oam/examples/example-agent-sandbox-confidential.yaml
```

### 3.3a Confidential mode without OAM — the verified procedure

Everything in 3.3 depends on the PR's ComponentDefinitions being deployed. This
variant needs none of that: a raw Pod, the real generator, and a real policy. It
is the shortest path to *confidential mode actually working*, and it is the one
that has been executed.

**Where the generator has to run.** The binaries are Linux ELF. On a Mac,
`gen-cedar-policy.sh --docker` handles it — but if Docker is not running, the
other option is a pod on the cluster itself. Note the arch: the download script
defaults to the **host** arch, so from an arm64 Mac you need
`--arch x86_64` for amd64 nodes.

```bash
# 1. the pod you want a policy for. enableServiceLinks: false is REQUIRED --
#    the confidential profile refuses a manifest without it.
cat > pod.yaml <<'EOF'
apiVersion: v1
kind: Pod
metadata:
  name: conf-demo
spec:
  runtimeClassName: confidential-sandbox
  restartPolicy: Never
  automountServiceAccountToken: false
  enableServiceLinks: false
  containers:
    - name: c
      image: public.ecr.aws/amazonlinux/amazonlinux:2023
      command: ["sh","-c","sleep infinity"]
      resources:
        requests: {cpu: "1", memory: 1Gi}
        limits:   {cpu: "1", memory: 1Gi}
EOF

# 2. generate. On a Linux host or Cloud Desktop:
bash scripts/nitro/download-runtime-binaries.sh
./.platform/private/nitro-sandbox-runtime/x86_64/eks-nitro-sandbox-genpolicy \
  --profile confidential pod.yaml
```

It prints the Cedar policy in clear, then a **Base64 Encoded Policy Envelope**,
then a **Predicted PCR17** and the KMS condition block. Put the base64 in the
pod as `sandbox.amazonaws.com/policy` and apply. **The pod reaching Running is
the test passing** — the policy was accepted, enforced and measured.

Three things this run established that are easy to get wrong:

- **`--with-pod-identity` must match the pod.** Passed for a pod with
  `automountServiceAccountToken: false` and no service account, the policy
  expects injected env the pod will not have, and the container is **denied**:

  ```
  create_container: PERMISSION_DENIED
    policy denied Agent::Action::"CreateContainer" on container "c"
  ```

  That is the policy working, not a bug. `gen-cedar-policy.sh` gets this right
  automatically by reading `serviceAccountName` off the rendered spec; a
  hand-rolled `genpolicy` invocation is where you get it wrong.

- **`--profile confidential` denies `exec` on its own.** With no `--deny-exec`
  passed at all:

  ```console
  $ kubectl exec conf-demo -- id
  error: ... policy denied Agent::Action::"Exec" on container "c"
  ```

  So exec-denial comes from the profile. `gen-cedar-policy.sh` still adds
  `--deny-exec --deny-logs` explicitly, because the profile's contents are not a
  documented contract.

- **On EKS Auto Mode, protect long-lived bare Pods.** Auto Mode consolidates
  nodes, and a bare Pod on a removed node is deleted rather than rescheduled —
  mid-test pods vanish with no obvious cause. Add
  `karpenter.sh/do-not-disrupt: "true"`, or use a Deployment/Job.

```bash
bash scripts/nitro/verify-isolation.sh conf-demo -n <ns>
```

Expect **5 PASS / 0 FAIL** and
`exec is DENIED, which is the confidential guarantee working` — the script
recognises the confidential case and tells you to verify the attestation (Part
4) instead of trying to look inside.

### 3.4 `example-sandbox-confidential.yaml` — a confidential warm pool

Same trust model, applied to a pool rather than an agent. Short, because 3.3
covered the mechanics.

| | |
|---|---|
| Application | `confidential-session-sandbox` |
| Component | `regulated-session` (`type: sandbox`) — renamed, see the warning above |
| Asks for | `mode: confidential` + `cedarPolicy`, `warmPool: 2`, `idleTimeout: 5m` |

```bash
docker buildx imagetools inspect public.ecr.aws/docker/library/python:3.12-slim | grep Digest
# update `image:` if it differs, then:
./scripts/nitro/gen-cedar-policy.sh \
  --app platform/oam/examples/example-sandbox-confidential.yaml \
  --component regulated-session --profile confidential
# paste into cedarPolicy, then:
kubectl apply -f platform/oam/examples/example-sandbox-confidential.yaml
```

```bash
kubectl get sandboxtemplate regulated-session-template \
  -o jsonpath='{.spec.podTemplate.spec.runtimeClassName}{"\n"}{.spec.podTemplate.spec.enableServiceLinks}'; echo
```

Expect `confidential-sandbox` and `false`. That `false` is not hardening — the
generator *refuses* a confidential manifest without it, because the kubelet
injects a `<SVC>_PORT` variable per Service in the namespace and the pinned env
set would not be knowable at generation time.

```bash
kubectl get configmap regulated-session-sandbox -o jsonpath='{.data.mode}'; echo   # confidential
kubectl get pods -l agent-sandbox.io/role=session                                    # 2, Running
```

**The one that matters here:** a confidential pool denies exec, and the Dark
Factory coder flow *drives* its sandbox over exec. So confirm exec is refused,
and understand that this pool shape is for a consumer that talks to the sandbox
over its own API — which is exactly why `nitro.mode` defaults to `isolated` for
the coder pool.

```bash
kubectl delete -f platform/oam/examples/example-sandbox-confidential.yaml
```

### 3.5 `example-agent-session-isolation.yaml` — per-session isolation

The feature issue #99 asked for: one microVM per (agent, user, session), rather
than one agent process shared by everyone.

| | |
|---|---|
| Application | `data-analyst-isolated` |
| Components | `data-analyst` (`type: agent`) + `analyst-session` (`type: sandbox`) |
| Asks for | `isolation: session`, `sandboxTemplate: analyst-session`, `sessionKey: subject+context` |
| Needs | `sessionIsolationEnabled: "true"` **and** the `agent_sandbox` addon |

**Step 1 — confirm the platform switch is on**, or the Application is rejected
with a message naming the prerequisite (which you can verify in Part 2):

```bash
kubectl get cm -n argocd -o yaml | grep -i sessionIsolationEnabled
kubectl get crd sandboxclaims.extensions.agents.x-k8s.io sandboxes.agents.x-k8s.io
```

**Step 2 — apply, and check both halves came up.** The agent is still a Rollout,
so blue-green survives; the pool is separate:

```bash
kubectl apply -f platform/oam/examples/example-agent-session-isolation.yaml
kubectl get rollout data-analyst
kubectl get sandboxwarmpool analyst-session \
  -o jsonpath='{.spec.replicas}'; echo                      # 3
kubectl get pods -l agent-sandbox.io/role=session             # 3, Running, pre-warmed
```

Note what the agent pods are **not**: the front door is an ordinary pod. The
microVM is the *session*, not the agent.

```bash
kubectl get pods -l app.kubernetes.io/name=data-analyst \
  -o jsonpath='{.items[0].spec.runtimeClassName}'; echo     # empty -- correct
```

**Step 3 — the four env vars are the contract with the image.** If the image
does not read these, nothing claims anything:

```bash
kubectl get rollout data-analyst -o jsonpath=\
'{range .spec.template.spec.containers[0].env[*]}{.name}={.value}{"\n"}{end}' \
  | grep -E 'AGENT_ISOLATION|SANDBOX_WARM_POOL|SANDBOX_NAMESPACE|AGENT_SESSION_KEY'
```

```
AGENT_ISOLATION=session
SANDBOX_WARM_POOL=analyst-session
SANDBOX_NAMESPACE=default
AGENT_SESSION_KEY=subject+context
```

**Step 4 — the RBAC, without which those vars are decoration.** A missing
RoleBinding is a 403 at claim time, long after deploy:

```bash
kubectl auth can-i create sandboxclaims \
  --as=system:serviceaccount:default:data-analyst         # yes
kubectl auth can-i create sandboxes \
  --as=system:serviceaccount:default:data-analyst         # no -- read-only by design
```

**Step 5 — the actual test: two users, two sandboxes.** Drive prompts through
the gateway as **two different authenticated subjects**, then:

```bash
kubectl get sandboxclaims -o custom-columns=\
'NAME:.metadata.name,PHASE:.status.phase,SANDBOX:.status.sandboxRef.name,AGE:.metadata.creationTimestamp'
```

Expect **two distinct claims**, each bound to a **different** sandbox. One claim
for two users means sessions are being shared — and see the limitation below
before blaming the platform.

Then confirm the isolation is real, not just two objects:

```bash
for s in $(kubectl get sandboxclaims -o jsonpath='{range .items[*]}{.status.sandboxRef.name}{" "}{end}'); do
  pod=$(kubectl get sandbox "$s" -o jsonpath='{.status.podRef.name}')
  echo "$s -> $pod  kernel: $(kubectl exec "$pod" -- uname -r 2>/dev/null)"
done
```

Write a file in one session and confirm it is **not** visible in the other —
separate kernels mean separate filesystems:

```bash
kubectl exec <pod-a> -- sh -c 'echo secret-a > /workspace/probe.txt'
kubectl exec <pod-b> -- cat /workspace/probe.txt      # must fail: No such file
```

**Step 6 — `sessionKey` semantics.** `subject+context` means the same user in a
*new* conversation gets a *new* sandbox. Drive the same user through two
conversations and expect two claims; switch the example to
`sessionKey: subject`, re-apply, and the same user should reuse one.

**Step 7 — idle teardown.** `idleTimeout: 15m`, `maxLifetime: 2h`. Leave a
session alone and confirm the sandbox is destroyed and the pool refills to 3 —
sandboxes that never get reclaimed are a capacity leak that only shows up under
load.

```bash
kubectl get sandbox,sandboxclaim
kubectl get pods -l agent-sandbox.io/role=session
kubectl delete -f platform/oam/examples/example-agent-session-isolation.yaml
```

> **Limitation, and the first thing to check if step 5 gives you one sandbox.**
> `sessionIsolationEnabled` is a *declarative* switch. It asserts the cluster has
> the addon **and** an agent image that claims a sandbox per session. Nothing
> verifies the second half — an agent on an image that never calls `SandboxClaim`
> renders, deploys, serves traffic and shares one process between every user,
> which is the exact failure #99 exists to remove. If you see one sandbox for two
> users, check the image before the platform. This is recorded as open in
> NITRO-DESIGN §9.4.

### 3.6 `examples/nitro-sandbox/pod-attested.yaml` — raw, no OAM

The attestation example, as a plain Pod. Covered by Part 4, because without the
KMS key policy there is nothing checking the measurements. Two things specific to
it:

```bash
# the ServiceAccount is a SEPARATE file on purpose: genpolicy takes a POD
# manifest and exits 1 on `unsupported kind: ServiceAccount`
kubectl apply -f examples/nitro-sandbox/serviceaccount-attested.yaml \
               -f examples/nitro-sandbox/pod-attested.yaml
```

And generate its policy from the **live pod**, not the file — the only genuinely
post-webhook source, because mutating webhooks run on the Pod rather than on
whatever produced it:

```bash
./scripts/nitro/gen-cedar-policy.sh --from pod/attested-demo -n default \
  --profile confidential --out from-live.b64
diff <(cat attested-demo-cedar-policy.b64) from-live.b64
```

A difference is your admission chain mutating the pod — Pod Identity's injected
env and token volume are the usual cause — and in confidential mode that
difference is the gap between a pod that starts and a pod that is denied.

### 3.7 Kata, on the same examples

If you have a Kata node pool, this is the portability claim: **the same
Applications, unchanged**, with one annotation on the cluster secret.

```
sandbox_runtime_class: kata-clh
confidential_sandbox_runtime_class:        # EMPTY: Kata cannot attest
nitro_sandbox_runtime_classes:             # EMPTY: no Nitro-only features here
sandbox_runtime: kata                      # the agent-sandbox warm pool
```

```bash
kubectl apply -f platform/oam/examples/example-agent-sandbox.yaml
kubectl get pods -l app.kubernetes.io/name=oap-assistant-sbx \
  -o jsonpath='{.items[0].spec.runtimeClassName}'; echo      # kata-clh
bash scripts/nitro/verify-isolation.sh -l app.kubernetes.io/name=oap-assistant-sbx
```

The kernel check passes the same way — Kata gives a separate kernel per pod too.
What Kata does **not** give is attestation, so both confidential examples must be
**rejected** here rather than quietly handed a `kata-clh` pod (Part 2.4), and
`example-sandbox-isolated.yaml` should come up on `kata-clh` with its warm pool
Running rather than Pending — the finding-1 regression.

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
| ☐ | `pod-hello-sandbox` Ready; kernel ≠ node, `.enclave`, virtio disks | 3.0 |
| ☐ | isolated agent: 2 pods on `isolated-sandbox`, own kernel, agent still answers | 3.1 |
| ☐ | isolated pool: 2 warm pods, claim binds in seconds, pool refills, idles out | 3.2 |
| ☐ | **confidential agent: placeholder policy REFUSED before the real one works** | 3.3 |
| ☐ | **confidential WITHOUT OAM: real policy generated, pod Running, exec denied** | 3.3a |
| ☐ | `--with-pod-identity` mismatch → `CreateContainer` denied (policy working) | 3.3a |
| ☐ | `verify-isolation.sh` on a confidential pod → 5 PASS, exec-denial reported as the guarantee | 3.3a |
| ☐ | confidential agent: exec denied; a spec change without regen is denied | 3.3 |
| ☐ | confidential pool: `enableServiceLinks: false`, exec denied | 3.4 |
| ☐ | session: 4 env vars, RBAC `can-i`, **two users → two sandboxes, separate FS** | 3.5 |
| ☐ | session: `sessionKey` semantics, idle teardown, pool refill | 3.5 |
| ☐ | `--app` vs `--from pod/` policies diffed | 3.6 |
| ☐ | Kata: same Applications unchanged, `kata-clh`, confidential rejected | 3.7 |
| ☐ | wrong guest denied | 4 |
| ☐ | **administrator denied** | 4 |
