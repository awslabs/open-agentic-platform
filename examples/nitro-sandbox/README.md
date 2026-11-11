# Nitro Sandbox attestation — a working end-to-end example

**What this shows:** a workload that can only start if the AWS Nitro hypervisor
attests that the exact guest image and the exact Cedar policy you expect are what
booted. Not "isolation is configured" — *attestation verified by something other
than the thing being attested*.

**Why it matters.** `sandboxMode: confidential` on its own gives you memory
isolation from the node: a cluster administrator cannot read the pod's memory,
the overlay is encrypted, exec and stdin are denied from the host. That is real,
and it is **not** confidentiality yet. The Nitro Secure Module measures the guest
and signs an attestation document, but if nothing ever *checks* those
measurements, they are a number written down and never read. A compromised or
dishonest parent instance can present a different policy; nobody notices.

The check is an **AWS KMS key policy** with `kms:RecipientAttestation` conditions.
KMS validates the attestation document and releases the key only when the
measurements match. This directory builds exactly that, and makes the result
observable: **the pod starts, or it does not.**

```
                   ┌─────────────────────────────────────────┐
                   │ parent instance (UNTRUSTED)             │
                   │                                         │
  encrypted image  │  ┌───────────────────────────────────┐  │
  layers ──────────┼─▶│ Nitro Sandbox (the trust boundary)│  │
  (operator can    │  │                                   │  │
   see ciphertext  │  │  NSM measures: PCR0 system image  │  │
   only)           │  │                 PCR8 EIF signer   │  │
                   │  │                 PCR17 Cedar policy│  │
                   │  │            │                      │  │
                   │  └────────────┼──────────────────────┘  │
                   └───────────────┼─────────────────────────┘
                                   │ kms:Decrypt + attestation doc
                                   ▼
                        ┌─────────────────────────┐
                        │ AWS KMS                 │
                        │ key policy requires     │
                        │  PCR17 == <your policy> │
                        │  PCR8:ServiceName ==    │
                        │    eks.amazonaws.com    │
                        └─────────────────────────┘
                          match ──▶ layer key released ──▶ pod starts
                          no match ─▶ AccessDenied ──▶ pod NEVER starts
```

Using **encrypted container images** as the demonstration is deliberate. It is
the one attestation-gated path AWS documents end to end today, and it has the
property a demo needs: the gate is on the critical path. The sandbox cannot
unpack the image without a successful attested `kms:Decrypt`, so a pod that runs
*is* a verified attestation, and there is nothing to take on trust.

> **On application-level attestation.** Having your own code fetch an
> attestation document and present it to KMS for an application secret is the
> same mechanism, but the in-guest API for it is not in the public documentation
> yet (AWS has it deferred to a follow-up deep-dive). This example does not
> invent one. What is here is complete and real; when that API is published, the
> KMS key policy in this directory is the part you keep.

## What's here

| Path | Role |
|---|---|
| `pod-hello-sandbox.yaml` | Smoke test. Confirms the cluster can run a Nitro Sandbox at all, before any KMS or policy work. |
| `create-attested-key.sh` | Creates the KMS key whose policy pins `PCR17` and `PCR8:ServiceName`. |
| `kms-key-policy.json` | The key policy, with placeholders. Read this one even if you use the script — the conditions are the whole point. |
| `encrypt-image.sh` | Encrypts an image's layers to that key with `skopeo` + `nitro-sandbox-keyprovider`. |
| `pod-attested.yaml` | The confidential pod running the encrypted image. Starts only if attestation passes. **Pod only, deliberately** — `genpolicy` takes a pod manifest and exits 1 on `unsupported kind: ServiceAccount`. |
| `serviceaccount-attested.yaml` | The pod's ServiceAccount, in its own file for the reason above. Apply it first. |

Related, outside this directory:

- `scripts/nitro/download-runtime-binaries.sh` — gets `eks-nitro-sandbox-genpolicy`
  and `nitro-sandbox-keyprovider`, signature-verified.
- `scripts/nitro/gen-cedar-policy.sh` — generates the policy and prints the
  PCR17 value this key policy needs.
- `scripts/nitro/verify-isolation.sh` — proves a running pod is really in a microVM.
- `platform/oam/examples/example-agent-sandbox-confidential.yaml` and
  `example-sandbox-confidential.yaml` — the same thing as OAM, for an agent and
  for a per-session sandbox.

## Prerequisites

1. **A cluster that can run Nitro Sandboxes.** Kubernetes **1.36+**, a US
   commercial region, and the classes present:

   ```console
   $ kubectl get runtimeclass isolated-sandbox confidential-sandbox
   NAME                   HANDLER                AGE
   confidential-sandbox   confidential-sandbox   5m
   isolated-sandbox       isolated-sandbox       5m
   ```

   On **EKS Auto Mode** that is all: a sandbox-capable node is provisioned on
   demand. Outside Auto Mode you need a Nitro Sandbox AMI and nodes launched with
   `EnclaveOptions.Enabled=true` — choosing an Enclaves-capable instance type is
   **not** sufficient on its own; without Enclaves enabled the node never
   advertises sandbox capacity.

2. **`skopeo`**, and the runtime binaries on your build machine:

   ```console
   $ scripts/nitro/download-runtime-binaries.sh
   $ export PATH="$PWD/.platform/private/nitro-sandbox-runtime/$(uname -m)/:$PATH"
   ```

   The binaries are Linux ELF (glibc 2.34+). On macOS use a Linux host or a
   container for the encryption and policy-generation steps.

3. **EKS Pod Identity** for the pod's ServiceAccount, with `kms:Decrypt` on the
   key. **IRSA is not used for image decryption** — it has to be Pod Identity.
   IMDS inside a sandbox serves only placement metadata; IAM credentials are not
   available through it.

4. An **ECR repository** (or any registry you can push to) for the encrypted
   image.

## Step 0 — smoke test the cluster

Do this first. Everything after it is harder to debug, and this one step
separates "my attestation is wrong" from "my cluster cannot run sandboxes".

```console
$ kubectl apply -f examples/nitro-sandbox/pod-hello-sandbox.yaml
$ kubectl wait --for=condition=Ready pod/hello-sandbox --timeout=5m
$ kubectl logs hello-sandbox
/sys/class/dmi/id/product_name:  m6i.enclave
disks:   vda vdb vdc
```

The `.enclave` suffix and the virtio disks are the sandbox. A normal pod reports
the instance type with no suffix and `nvme0n1 nvme1n1 zram0`. For the decisive
check — a guest kernel that differs from the node's — run:

```console
$ scripts/nitro/verify-isolation.sh hello-sandbox
```

If the pod stays `Pending`, that is the designed failure rather than a silent
downgrade. `kubectl describe pod hello-sandbox` names the reason; on Auto Mode
the usual causes are a cluster below 1.36, a NodeClass using FIPS AMIs, or a
GPU/Neuron/multi-NUMA instance type, none of which Auto Mode will provision as a
sandbox node.

Clean up: `kubectl delete -f examples/nitro-sandbox/pod-hello-sandbox.yaml`

## Step 1 — generate the Cedar policy, and read the PCR17 it predicts

The policy must be generated from the **final** pod spec. Generate it from
`pod-attested.yaml` *after* filling in your image reference:

```console
$ eks-nitro-sandbox-genpolicy --profile confidential \
    --with-pod-identity \
    examples/nitro-sandbox/pod-attested.yaml
```

Two flags that are not optional here:

- `--with-pod-identity`, because the guest needs the Pod Identity token to call
  KMS for the layer key. Without it the policy denies the very call the pod
  depends on. It is needed for a second reason too: Pod Identity *injects*
  `AWS_CONTAINER_CREDENTIALS_FULL_URI`, a token-file variable and a projected
  volume, and `--profile confidential` pins the environment exactly — so a
  policy generated without this flag denies the pod over an env mismatch.
- `--profile confidential`, which pins image digest, args, environment, mounts,
  capabilities and security settings.

Two things about the manifest, both of which the generator enforces:

- **`enableServiceLinks: false` is required**, not hardening. The kubelet injects
  a `<SVC>_PORT…` variable per Service in the namespace, so the pinned env set is
  not knowable at generation time; `--profile confidential` refuses a manifest
  that leaves it unset. `pod-attested.yaml` sets it, and OAP emits it on every
  confidential pod template. The only other way past it is
  `--allow-additional-env`, which stops pinning the environment — most of what
  the confidential profile is for.
- **One Pod per file.** The generator takes a *pod* manifest and exits 1 with
  `unsupported kind: ServiceAccount` on a multi-document file, which is why the
  ServiceAccount lives in `serviceaccount-attested.yaml`. Apply both:

  ```console
  $ kubectl apply -f examples/nitro-sandbox/serviceaccount-attested.yaml \
                  -f examples/nitro-sandbox/pod-attested.yaml
  ```

It prints the base64 policy **and the predicted PCR17**. That prediction is the
useful part: you can configure KMS before anything is deployed, rather than
deploying, failing, reading a value out of a running sandbox and trying again.

For an OAP component, use the wrapper instead — it extracts the pod template
KubeVela actually renders:

```console
$ scripts/nitro/gen-cedar-policy.sh \
    --app platform/oam/examples/example-agent-sandbox-confidential.yaml \
    --component regulated-assistant \
    --profile confidential
```

No `-- --with-pod-identity`: the wrapper reads `serviceAccountName` off the
rendered pod spec and adds it, and adds `--deny-exec --deny-logs` on the
confidential profile (see *What confidential does not protect* below).
`--no-pod-identity` and `--allow-exec-logs` opt out of each.

**Then regenerate from the running pod and diff the two.** `--app` renders
offline, which is what makes it usable in CI, and offline is exactly what cannot
see a mutating webhook. `--from pod/<name>` is the only post-mutation source:

```console
$ scripts/nitro/gen-cedar-policy.sh --from pod/attested-demo -n default \
    --profile confidential --out from-live.b64
$ diff <(cat regulated-assistant-cedar-policy.b64) from-live.b64
```

A difference means your admission chain is changing the pod, and in confidential
mode that difference is the gap between a pod that starts and a pod that is
denied.

## Step 2 — create the KMS key with the attestation conditions

```console
$ examples/nitro-sandbox/create-attested-key.sh \
    --pcr17 <the value from step 1> \
    --role-arn arn:aws:iam::111122223333:role/<the pod's Pod Identity role> \
    --yes
```

The condition block it writes, which is the entire point of this example:

```json
"Condition": {
  "StringEqualsIgnoreCase": {
    "kms:RecipientAttestation:PCR17": "<sha-384 of your Cedar policy>",
    "kms:RecipientAttestation:PCR8:ServiceName": "eks.amazonaws.com"
  }
}
```

**Both conditions, not one.** `PCR17` is the measurement of your Cedar policy, so
a modified policy produces a different value and KMS refuses. But PCR17 alone
would accept that same policy hash presented from a guest image AWS never
signed. `PCR8` measures the certificate that signed the guest image (the EIF),
and the `:ServiceName` selector compares the service identity recorded in that
certificate rather than the raw hash — so pinning it to `eks.amazonaws.com`
attests that the guest image was signed by the EKS Nitro Sandbox service, and it
keeps matching when AWS rotates the signing certificate. Pinning a raw PCR8 hash
breaks on rotation; pinning nothing breaks the guarantee.

**And a Deny, which is what makes the Allow mean anything.** A KMS key policy is
the root of authorization for its key, so the administration statement every key
needs is also the hole: granted as `kms:*`, it delegates `kms:Decrypt` to the
account, and any administrator can then write themselves an IAM policy and
unwrap the layer key with no attestation whatsoever — the exact actor
confidential mode exists to exclude. So `kms-key-policy.json` does two things:
the administration statement no longer includes the data-plane actions, and an
explicit `Deny` catches anything that still reaches them without a matching
measurement.

```json
{
  "Sid": "DenyDataPlaneWithoutAttestation",
  "Effect": "Deny",
  "Principal": "*",
  "Action": ["kms:Decrypt", "kms:GenerateDataKey", "..."],
  "Resource": "*",
  "Condition": {
    "StringNotEqualsIfExists": {
      "kms:RecipientAttestation:PCR17": "<sha-384 of your Cedar policy>"
    }
  }
}
```

`Deny` beats every `Allow` — in the key policy, in IAM, and in grants — so this
holds even if a future statement or a `kms:CreateGrant` adds a path nobody
remembers. The `IfExists` suffix is load-bearing: it makes the condition true
when the attestation key is **absent**, which is precisely the ordinary caller
with no attestation document. The attested sandbox presents a matching `PCR17`,
so the Deny does not apply to it.

`PCR0` measures the system image itself and can be added when you want to pin a
specific guest image version. Be aware of what that costs: every AWS update to
the guest image changes PCR0, and a key policy pinned to the old value stops
releasing keys until you update it.

## Step 3 — encrypt the image

```console
$ examples/nitro-sandbox/encrypt-image.sh \
    --source public.ecr.aws/amazonlinux/amazonlinux:2023 \
    --dest 111122223333.dkr.ecr.us-west-2.amazonaws.com/attested-demo:encrypted \
    --key-arn arn:aws:kms:us-west-2:111122223333:key/<id>
```

**Every layer must be encrypted.** A mixed image is not supported: the pull fails
naming the unencrypted layers, and `genpolicy` refuses to generate a policy for
it. `skopeo copy` as the script runs it encrypts all of them.

The `--encryption-key` argument needs the **full key ARN**. Bare key IDs and
aliases are rejected, because the ARN is written into the encrypted image so the
guest knows which regional KMS endpoint to call.

## Step 4 — deploy, and watch the gate work

Put the base64 policy from step 1 into `pod-attested.yaml` as
`sandbox.amazonaws.com/policy`, point the image at your encrypted tag, then:

```console
$ kubectl apply -f examples/nitro-sandbox/pod-attested.yaml
$ kubectl get pod attested-demo -w
```

**Running** means the sandbox called `kms:Decrypt` with its attestation document,
KMS compared the measurements against the key policy, released the layer key, and
the guest decrypted and unpacked the image. The pod running *is* the verification.

### The negative control, which is the part worth doing

A gate you have never seen reject anything is not known to be a gate. Change one
byte of the Cedar policy — regenerate it with a different flag, say `--deny-logs`
— and apply the pod with the new policy but the **old** PCR17 still in the key
policy:

```console
$ kubectl describe pod attested-demo | grep -A5 Events
  Warning  Failed  ...  Error: failed to pull and unpack image ...
                        kms:Decrypt: AccessDeniedException
```

That `AccessDeniedException` is KMS refusing to release a key to a guest whose
measurements do not match. Nothing else in the stack can produce that error, and
seeing it once is worth more than reading this README.

### The second negative control: try it as an administrator

The control above proves the key refuses the *wrong guest*. It says nothing about
whether the key refuses **you** — and a key policy granting `kms:*` to the
account root refuses nobody, while looking identical from the outside. Check it
with your own admin credentials and no attestation at all:

```console
$ aws kms encrypt --key-id <key-arn> --plaintext $(echo -n probe | base64) \
    --query CiphertextBlob --output text | base64 -d > /tmp/probe.bin
$ aws kms decrypt --key-id <key-arn> --ciphertext-blob fileb:///tmp/probe.bin

An error occurred (AccessDeniedException) when calling the Decrypt operation: ...
```

(The `encrypt` call is expected to fail too unless you are the build principal —
in that case borrow a ciphertext from the encrypted image, or run the `decrypt`
against any blob at all. Authorization is evaluated **before** the ciphertext is
examined, which is the whole trick below.)

**Read the error, not just the failure.** `AccessDeniedException` is the pass.
`InvalidCiphertextException` is a **FAIL**: it means authorization succeeded and
KMS got as far as looking at your bytes, so this key will decrypt for an
administrator. Plaintext back is obviously a fail. Both mean the Deny statement
is missing or its `PCR17` does not match the one in the Allow.

## What confidential mode does not protect

The sections above are about what attestation *buys*. This one is the boundary of
it, because every limit here is reachable by someone a reader might assume
confidential mode had excluded. AWS lists these on the EKS Nitro Sandboxes
security-model page; the OAP-specific consequences are called out per item.

The property you get is narrow and real: **a ciphertext that decrypts only inside
a guest whose measurements match**. Not "nobody can see anything about this pod".

- **Anything the pod spec names, the parent can read.** Plain `env` values,
  mounted Secrets, CSI secrets, EBS and EFS volumes are all supplied *by* the
  untrusted side. **This hits OAP directly:** agents receive Langfuse keys as
  plain environment variables, so those are visible to anyone who can read the
  pod spec, regardless of the sandbox. Treat them as parent-visible, and keep
  anything that must not be, in a ciphertext this key gates.
- **Pod Identity credentials belong to the ServiceAccount, not to the guest.**
  Anyone who can run a pod with that service account mints the same credentials —
  from an ordinary runc pod, with no attestation. The attestation gate protects
  the *KMS key*, not the role. Scope the role to exactly the attested key, and
  restrict who may use the service account.
- **`kubectl exec` and `kubectl logs` go through the kubelet and work normally.**
  Unless the Cedar policy denies them, a namespace admin reads the agent's
  working memory by attaching to it. `scripts/nitro/gen-cedar-policy.sh` now adds
  `--deny-exec --deny-logs` by default on the confidential profile for this
  reason; `--allow-exec-logs` opts out, knowingly. Note the trade: a consumer
  that *drives* the sandbox over exec — the Dark Factory coder flow is one —
  cannot use the denying profile, which is why `nitro.mode` defaults to
  `isolated` for that pool.
- **Whoever can author pod specs in the namespace sets the measurements.** They
  choose the image, the args, the env and the Cedar policy, so they decide what
  gets attested. Restricting pod-spec authorship in the namespace is a
  prerequisite for this guarantee, not an optional hardening step.
- **Scheduling, lifecycle and metadata stay with the control plane.** Node
  placement, eviction, resource limits and every label and annotation are the
  parent's. Confidential mode hides memory contents, not the pod's existence or
  its shape.

Put the other way round: confidential mode moves the trust boundary off the node
and onto *the Kubernetes API server and whoever can write to it in this
namespace*. If that set is as large as your cluster administrators, the gate is
real but the blast radius has not moved much.

## Operational notes, each of which has cost somebody an afternoon

- **Regenerate the policy and the PCR17 condition together, in one change.** A
  new image digest alone invalidates the policy. A stale policy does not degrade
  into something weaker — the pod is **denied**. Put both in the CI job that
  produces your manifests.
- **Generate from the post-webhook spec.** If your cluster injects sidecars or
  rewrites pod specs, a policy generated from the pre-admission YAML will not
  match the pod that starts. `scripts/nitro/gen-cedar-policy.sh --from
  rollout/<name>` reads the live object for this reason.
- **A non-numeric `USER` in an encrypted image fails policy generation.**
  `genpolicy` cannot read `/etc/passwd` out of encrypted layers to resolve a
  name, so set `securityContext.runAsUser` and `runAsGroup` in the pod spec (both
  are set in `pod-attested.yaml`). Add `supplementalGroupsPolicy: Strict` to pin
  groups exactly, which tells the runtime to ignore the image's `/etc/group`.
- **`imageID` is synthetic for an encrypted image.** The digest `kubectl` and
  `crictl` report is derived by the runtime, not published by the registry, so it
  will not match `aws ecr describe-images`, `skopeo inspect` or your signer. Do
  not build drift detection on it.
- **Confidential mode denies exec and stdin.** If your runbook or your liveness
  probe depends on either, fix that before you adopt the mode. Logs still work.
- **Network isolation and attestation are mutually exclusive.**
  `sandbox.amazonaws.com/network-isolated: "true"` removes the route to KMS along
  with every other route, so the layer key can never be fetched. OAP rejects that
  combination at render time rather than letting the pod fail at pull.
- **Signature verification instead of a digest pin.** If regenerating on every
  rebuild is the part you cannot live with, sign the image with AWS Signer and
  generate the policy with `--require-any-signer <profile-arn>
  --skip-image-ref-container <name>`. Trust then comes from the signature and the
  image can evolve. The pod's role needs `signer:GetRevocationStatus` and
  `signer:GetSigningProfile`, and verification **fails closed** if either call is
  denied.

## Reading

- [Sandboxing on Amazon EKS with isolated and confidential pods](https://docs.aws.amazon.com/eks/latest/userguide/sandboxing.html)
- [Run a pod in confidential mode](https://docs.aws.amazon.com/eks/latest/userguide/sandboxing-confidential-setup.html)
- [Encrypted container images](https://docs.aws.amazon.com/eks/latest/userguide/sandboxing-encrypted-images.html)
- [Security model and parent instance trust assumptions](https://docs.aws.amazon.com/eks/latest/userguide/sandboxing-security-model.html)
- [AWS Nitro Sandboxes: hardware-enforced workload isolation on Amazon EKS](https://aws.amazon.com/blogs/containers/aws-nitro-sandboxes-hardware-enforced-workload-isolation-on-amazon-eks/)
- OAP design: [`docs/sandbox-agents/NITRO-DESIGN.md`](../../docs/sandbox-agents/NITRO-DESIGN.md)
