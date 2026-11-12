# The `isolation-matrix` CI workflow — what it checks, and what it cannot

`.github/workflows/isolation-matrix.yml`

## In one line

It checks that the **manifests the platform generates** are right and that the
**guardrails still bite** — it says nothing about whether a microVM is real.

## Why it exists

Sandbox isolation has one failure mode that review cannot catch, and it is the
one that matters: **isolation that is absent but looks present.**

An Application asks for `sandbox: true`. Something changes — a condition is
simplified, an annotation key is renamed, a default moves — and the pod now
renders without a runtime class, or with a class the cluster does not have, or
with the policy annotation on the wrong object. The agent still starts. It still
answers. Every dashboard is green. Nothing anywhere reports a problem, because
from the outside a pod with no isolation and a pod with isolation look identical.

That is not hypothetical. It is what this feature's review actually found:

- `sandboxMode: confidential` without `sandbox: true` rendered an **ordinary
  shared-kernel pod** — no runtime class, no attestation — and reported success.
- `networkIsolated: true` on a Kata class published `networkIsolated: "true"` on
  the agent's card while the pod had **full network access**.
- The `agent-sandbox` chart's default moved from Kata to Nitro with no
  per-cluster knob, so warm pools were recreated on a class their nodes did not
  have and every pod went `Pending`.
- The committed `agent.yaml` drifted from `agent.cue` on `main`, which left
  `sandbox: true` **broken on the hub** while ArgoCD reported Synced/Healthy.

A diff does not show any of these. A test that tries the combination does.

## The four things it actually checks

### 1. Does each feature emit what its consumer reads?

`scripts/test-isolation-features.sh` — **25 rows**, sections F and H.

Not "did it render" but "is the content right":

- the runtime class per trust model (`isolated-sandbox` / `confidential-sandbox`)
- the Cedar policy landing on the **pod template's** annotations, not the Rollout's
- annotations under the **exact key names the Nitro runtime reads** — e.g.
  `sandbox.amazonaws.com/dns-packets-per-second`, which is *not* the parameter's
  name, so a rename is invisible in review and silently drops the setting
- `enableServiceLinks: false` present for confidential and **absent** for isolated
- `type: sandbox`: `warmPool` reaching the pool's replicas, the template ref, no
  SA token by default, `service: false` emitting no Service, and the card
  reporting back the class and mode actually in force

### 2. Do the forbidden combinations still fail?

`scripts/test-isolation-matrix.sh` — **49 rows** (4 SKIP), sections A–D.

| | |
|---|---|
| **A** | every shipped example still renders |
| **B** | each capability still composes *alongside* the others in one Application — isolation touches the pod template, which is also where memory, evaluation, MCP and the identity traits land, and "renders alone" is not "composes" |
| **C** | every guard still **rejects** — and asserts the guard's **own name**, so a row cannot pass on an unrelated rejection |
| **D** | no two components or shipped examples emit the same `(kind, namespace, name)` — the class of bug that renders perfectly and breaks on apply, when whichever Application syncs last owns a resource the other still believes it manages |

Section C is the reason the workflow exists. A guard that stops firing is worse
than one that never existed, because the thing it protected still looks fine.

**Worth doing once, to see it work:** delete a guard in a scratch copy and watch
the row flip. The output is the argument for asserting names:

```
FAIL  C12  agent: sandboxMode without sandbox:true
           expected=_sandboxModeNeedsSandbox actual=reject:_cedarPolicyNeedsSandbox
```

The Application was *still rejected* — by a sibling guard — so a row asserting
only "reject" would have **passed** with the guard under test deleted.

### 3. Does the Helm chart branch correctly?

The `agent-sandbox` chart is Helm, not CUE, and has its own runtime split:

- the default is **Nitro**, and the pod carries **no Kata nodeSelector** (both
  labels on one pod is what made every warm pod `Pending`)
- `runtime: kata` still selects `kata-clh` **with** its nodeSelector and toleration
- an unknown runtime **fails** instead of falling through to Kata — isolation
  chosen by typo was possible before
- `warmPool.enabled=false` drops only the pool, keeping the operator and template
- confidential emits `enableServiceLinks: false`, which the real policy generator
  *requires* and refuses a manifest without

### 4. Is the committed YAML in step with the CUE?

The chart ships **generated** `agent.yaml` / `sandbox.yaml`, not the `.cue`
files. A stale committed template is deployable while the CUE says something
else, and nothing complains. `main` has exactly that drift today. The step
regenerates every definition and diffs it, printing the regeneration command when
it finds a difference.

## What it cannot tell you

All of it is manifest-level. The job never starts a pod, never contacts a
cluster, and needs no AWS credentials.

**It cannot tell you isolation works.** No kernel comparison, no DMI check, no
attestation. A pod could render perfectly and run with no isolation at all and
this workflow would stay green.

That is what [`TESTING.md`](TESTING.md) Part 3 is for, and only real hardware can
answer it: the pod reporting a different kernel from its node, `DMI *.enclave`,
and — for confidential — an attested `kms:Decrypt` that releases a key only to a
guest whose measurements match.

| Question | Answered by |
|---|---|
| Is the generated manifest right? | this workflow |
| Do the guardrails still reject? | this workflow |
| Is the microVM real? | TESTING.md Part 3, on Nitro nodes |
| Does attestation gate a secret? | TESTING.md Part 4, with a KMS key |

## When it runs, and what it needs

Pull requests and pushes to `main` touching `platform/oam/definitions/**`,
`platform/oam/examples/**`, either sandbox chart, or the two test scripts. Also
`workflow_dispatch`. One `ubuntu-latest` job, 20-minute timeout,
`contents: read`, about 90 seconds in practice.

It installs `helm`, the `vela` CLI and `PyYAML`, and writes a **throwaway
kubeconfig**. That last one is not optional and is worth understanding: `vela
dry-run --offline` builds a client *before* it honours `--offline`, and exits 1
with empty stdout when there is no readable kubeconfig. Inside the matrix that
failure is indistinguishable from a guard firing, so **every guard row would pass
and the rest would fail**. The file is never contacted; it only has to parse. The
matrix has a preflight that catches a missing one and says so.

## Running it yourself

```bash
bash scripts/test-isolation-features.sh    # expect: all feature rows passed
bash scripts/test-isolation-matrix.sh      # expect: failed rows: 0
bash scripts/test-isolation-matrix.sh -v   # with each rejection's message
```

Needs a readable kubeconfig for the reason above — any valid file will do.

> **If you change these scripts, test them with `TMPDIR` unset.** The preflight
> originally wrote to `"$TMPDIR/..."`, which is set on macOS and **unset on a
> Linux runner**; under `set -u` that aborted the whole suite before a single row
> ran, and it passed locally every time. Reproduce CI with:
> ```bash
> env -u TMPDIR bash -e -c 'bash scripts/test-isolation-matrix.sh -v'
> ```
