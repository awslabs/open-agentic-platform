#!/usr/bin/env bash
# Encrypt every layer of a container image to a KMS key, so the image's contents
# are opaque to the parent instance and can only be unpacked inside an attested
# Nitro Sandbox.
#
# What this buys you beyond confidential mode's memory isolation: the layer blobs
# the node handles are ciphertext, so the node operator never sees the image's
# contents -- and because the layer key comes from an attested kms:Decrypt, the
# pod cannot start at all unless the attestation matched. The attestation gate
# sits on the critical path rather than beside it, which is what makes it
# verifiable: a running pod IS a verified attestation.
#
# usage:
#   examples/nitro-sandbox/encrypt-image.sh \
#     --source public.ecr.aws/amazonlinux/amazonlinux:2023 \
#     --dest   111122223333.dkr.ecr.us-west-2.amazonaws.com/attested-demo:encrypted \
#     --key-arn arn:aws:kms:us-west-2:111122223333:key/<id> \
#     [--keyprovider <path to nitro-sandbox-keyprovider>]
set -euo pipefail

SOURCE=""
DEST=""
KEY_ARN=""
KEYPROVIDER=""
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

while [ $# -gt 0 ]; do
  case "$1" in
    --source)       SOURCE="$2"; shift 2 ;;
    --dest)         DEST="$2"; shift 2 ;;
    --key-arn)      KEY_ARN="$2"; shift 2 ;;
    --keyprovider)  KEYPROVIDER="$2"; shift 2 ;;
    -h|--help)      sed -n '1,20p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

[ -n "$SOURCE" ]  || { echo "--source is required" >&2; exit 2; }
[ -n "$DEST" ]    || { echo "--dest is required" >&2; exit 2; }
[ -n "$KEY_ARN" ] || { echo "--key-arn is required" >&2; exit 2; }

# A FULL key ARN, not an ID or alias. The keyprovider takes the region for its
# kms:Encrypt call from the ARN and writes the ARN into the encrypted image, so
# the guest knows which regional endpoint to call for the decrypt. An alias is
# rejected because the recorded reference has to identify the key
# unambiguously -- an alias can be repointed, and then the image's recorded key
# is not the key its layers were wrapped with.
case "$KEY_ARN" in
  arn:aws*:kms:*:*:key/*) ;;
  *) echo "--key-arn must be a FULL key ARN (arn:aws:kms:<region>:<account>:key/<id>)." >&2
     echo "Bare key IDs and aliases are rejected by the keyprovider." >&2
     exit 2 ;;
esac

command -v skopeo >/dev/null || { echo "missing: skopeo (https://github.com/containers/skopeo)" >&2; exit 1; }

if [ -z "$KEYPROVIDER" ]; then
  case "$(uname -m)" in
    x86_64|amd64)  A=x86_64 ;;
    arm64|aarch64) A=aarch64 ;;
    *) A="" ;;
  esac
  if [ -n "$A" ] && [ -x "$REPO_ROOT/.platform/private/nitro-sandbox-runtime/$A/nitro-sandbox-keyprovider" ]; then
    KEYPROVIDER="$REPO_ROOT/.platform/private/nitro-sandbox-runtime/$A/nitro-sandbox-keyprovider"
  else
    KEYPROVIDER="$(command -v nitro-sandbox-keyprovider || true)"
  fi
fi
if [ -z "$KEYPROVIDER" ] || [ ! -x "$KEYPROVIDER" ]; then
  echo "nitro-sandbox-keyprovider not found. Get it with:" >&2
  echo "  scripts/nitro/download-runtime-binaries.sh" >&2
  exit 1
fi

if [ "$(uname -s)" != "Linux" ] &&
   [ "$(head -c 4 "$KEYPROVIDER" | od -An -c | tr -d ' \n')" = "177ELF" ]; then
  echo "$KEYPROVIDER is a Linux ELF binary and this is $(uname -s)." >&2
  echo "Run this step on a Linux host or in a Linux container -- skopeo has to be" >&2
  echo "able to exec the keyprovider." >&2
  exit 1
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# The ocicrypt keyprovider config. Written to a temp path and passed by env var
# rather than installed at /etc/ocicrypt.json, so this script needs no root and
# leaves nothing behind on a shared build host.
cat > "$WORK/ocicrypt.json" <<EOF
{
  "key-providers": {
    "awskms": {
      "cmd": {
        "path": "$KEYPROVIDER",
        "args": []
      }
    }
  }
}
EOF

echo "==> encrypting every layer"
echo "    source: $SOURCE"
echo "    dest:   $DEST"
echo "    key:    $KEY_ARN"
echo

# EVERY layer, which is why there is no selective flag here. Isolated and
# confidential pods do not support an image mixing encrypted and plaintext
# layers: the pull fails naming the plaintext ones, and genpolicy refuses to
# generate a policy for such an image at all. skopeo copy encrypts all of them.
OCICRYPT_KEYPROVIDER_CONFIG="$WORK/ocicrypt.json" \
  skopeo copy \
    --encryption-key "provider:awskms:$KEY_ARN" \
    "docker://$SOURCE" \
    "docker://$DEST"

echo
echo "encrypted: $DEST"
echo
echo "next:"
echo "  1. point pod-attested.yaml's image at it"
echo "  2. generate the policy from that manifest, WITH --with-pod-identity --"
echo "     the guest needs the Pod Identity token to call KMS for the layer key,"
echo "     and a policy generated without it denies that very call:"
echo "       eks-nitro-sandbox-genpolicy --profile confidential --with-pod-identity \\"
echo "         examples/nitro-sandbox/pod-attested.yaml"
echo "  3. put the base64 in the sandbox.amazonaws.com/policy annotation and the"
echo "     printed PCR17 in the KMS key policy, then apply."
echo
echo "NOTE: the imageID kubectl and crictl report for an encrypted image is"
echo "SYNTHETIC -- derived by the runtime, not published by the registry -- so it"
echo "will not match aws ecr describe-images or skopeo inspect. Do not build drift"
echo "detection on it."
