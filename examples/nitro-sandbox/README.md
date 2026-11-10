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
| `pod-attested.yaml` | The confidential pod running the encrypted image. Starts only if attestation passes. |

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
  depends on.
- `--profile confidential`, which pins image digest, args, environment, mounts,
  capabilities and security settings.

It prints the base64 policy **and the predicted PCR17**. That prediction is the
useful part: you can configure KMS before anything is deployed, rather than
deploying, failing, reading a value out of a running sandbox and trying again.

For an OAP component, use the wrapper instead — it extracts the pod template
KubeVela actually renders:

```console
$ scripts/nitro/gen-cedar-policy.sh \
    --app platform/oam/examples/example-agent-sandbox-confidential.yaml \
    --component regulated-assistant \
    --profile confidential -- --with-pod-identity
```

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
