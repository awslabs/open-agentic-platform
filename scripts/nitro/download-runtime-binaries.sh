#!/usr/bin/env bash
# Download and VERIFY the AWS Nitro Sandbox runtime binaries.
#
# What you actually came for is one of the three: eks-nitro-sandbox-genpolicy,
# which generates the Cedar policy a confidential pod needs. The archive also
# carries containerd-shim-nitro-sandbox-v2 and nitro-sandbox-keyprovider, which
# belong on a NODE and are only needed when you build your own Nitro Sandbox AMI
# (on EKS Auto Mode you never do).
#
# Verification is not optional here, whatever the AWS docs call it. The output of
# this tool becomes the policy that decides what may run inside a confidential
# sandbox and the PCR17 value a KMS key policy pins. A tampered generator
# produces a policy that looks right and attests to the wrong thing, which is
# exactly the attack confidential mode exists to stop. This script fails closed
# on any verification error and deletes what it downloaded.
#
# usage: scripts/nitro/download-runtime-binaries.sh [--arch x86_64|aarch64]
#                                                   [--version latest|<x.y.z.w>]
#                                                   [--dest <dir>]
#
# Puts the binaries in <dest> (default .platform/private/nitro-sandbox-runtime,
# which is gitignored) and prints the PATH line to use.
set -euo pipefail

ARCH=""
VERSION="latest"
DEST=""

while [ $# -gt 0 ]; do
  case "$1" in
    --arch)    ARCH="$2"; shift 2 ;;
    --version) VERSION="$2"; shift 2 ;;
    --dest)    DEST="$2"; shift 2 ;;
    -h|--help) sed -n '1,25p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

# Default to the architecture of the machine running this, because that is where
# genpolicy runs -- NOT the architecture of the cluster nodes. The two differ
# routinely (an arm64 laptop against amd64 nodes) and picking the node's
# architecture gets you a binary that will not execute.
if [ -z "$ARCH" ]; then
  case "$(uname -m)" in
    x86_64|amd64)  ARCH=x86_64 ;;
    arm64|aarch64) ARCH=aarch64 ;;
    *) echo "cannot map $(uname -m) to a published architecture; pass --arch" >&2; exit 1 ;;
  esac
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEST="${DEST:-$REPO_ROOT/.platform/private/nitro-sandbox-runtime/$ARCH}"
BASE="https://d1uvmbvklg8rt.cloudfront.net/$VERSION/$ARCH"

for t in curl openssl tar; do
  command -v "$t" >/dev/null || { echo "missing: $t" >&2; exit 1; }
done
# sha256sum on Linux, shasum -a 256 on macOS. The manifest format is the
# coreutils one, which shasum also reads with -c.
if command -v sha256sum >/dev/null; then SHACHECK=(sha256sum -c)
elif command -v shasum >/dev/null;   then SHACHECK=(shasum -a 256 -c)
else echo "missing: sha256sum or shasum" >&2; exit 1; fi

WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

echo "==> downloading $VERSION/$ARCH"
curl -fsSL -o "$WORK/nitro-sandboxes-runtime.tar.gz" "$BASE/nitro-sandboxes-runtime.tar.gz"
# The manifest DOWNLOADED ALONGSIDE the archive is the one AWS signs. The archive
# contains a file of the same name covering a different set of files; verifying
# against that one proves nothing and fails anyway. Download first, extract
# second, and never let the extraction overwrite this.
curl -fsSL -o "$WORK/SHA256SUM"     "$BASE/SHA256SUM"
curl -fsSL -o "$WORK/SHA256SUM.sig" "$BASE/SHA256SUM.sig"

# The public half of the asymmetric AWS KMS key AWS signs releases with (ECDSA,
# NIST P-384). Published in the EKS user guide, "Download and verify the runtime
# binaries". Pinned here on purpose: fetching the key over the same channel as
# the artifact would verify nothing.
cat > "$WORK/nitro-sandboxes-runtime-v1.pub" <<'PUB'
-----BEGIN PUBLIC KEY-----
MHYwEAYHKoZIzj0CAQYFK4EEACIDYgAESqdVXO82n+Zh5+IFLh1xjmxZLkjjZWvf
DCS927SdVYjP0AZ92XCxS+ucrs9cjuNlTaDARTHov2XVfMVgau8TEPFKGDRmVZXJ
c25CVihKHLFK8T8hNMIL+fbGcvE2fwIO
-----END PUBLIC KEY-----
PUB

echo "==> verifying the signature over the manifest"
# SHA-384, pairing with the P-384 key -- even though the hashes INSIDE the
# manifest are SHA-256. Using -sha256 here fails with a signature error that
# looks like tampering.
if ! openssl dgst -sha384 -verify "$WORK/nitro-sandboxes-runtime-v1.pub" \
      -signature "$WORK/SHA256SUM.sig" "$WORK/SHA256SUM"; then
  echo "SIGNATURE VERIFICATION FAILED -- this is not what AWS published. Nothing written." >&2
  exit 1
fi

echo "==> extracting"
# The archive extracts flat. Into a subdirectory so its own SHA256SUM cannot
# clobber the signed manifest sitting next to it.
mkdir -p "$WORK/x"
tar -xzf "$WORK/nitro-sandboxes-runtime.tar.gz" -C "$WORK/x" 2>/dev/null ||
  tar -xzf "$WORK/nitro-sandboxes-runtime.tar.gz" -C "$WORK/x"
# 'implausibly old time stamp' warnings are expected: the archive uses fixed
# timestamps for reproducible builds. tar still exits 0.

echo "==> verifying the archive and each binary against the signed manifest"
# The manifest covers the archive AND each binary, so it is checked from a
# directory holding both.
cp "$WORK/nitro-sandboxes-runtime.tar.gz" "$WORK/x/"
if ! ( cd "$WORK/x" && "${SHACHECK[@]}" "$WORK/SHA256SUM" ); then
  echo "CHECKSUM VERIFICATION FAILED -- at least one file is not what AWS published. Nothing written." >&2
  exit 1
fi

mkdir -p "$DEST"
# Install EVERY file the signed manifest covered, rather than the three the user
# guide names. The release in fact ships more -- nitro-sandbox-agent,
# nitro-sandbox-shim-logger-wrapper, ecs-nitro-sandbox-genpolicy and
# nitro-sandbox-policy-eval, which evaluates a generated policy against a pod
# spec -- and a hardcoded list silently drops whatever AWS adds next. Everything
# here is already verified, so copying all of it costs nothing.
rm -f "$WORK/x/nitro-sandboxes-runtime.tar.gz" "$WORK/x/SHA256SUM"
for f in "$WORK/x"/*; do
  b="$(basename "$f")"
  case "$b" in
    LICENSE|ATTRIBUTION|*.md|*.txt) install -m 0644 "$f" "$DEST/" ;;
    *)                              install -m 0755 "$f" "$DEST/" ;;
  esac
done
# Keep the signed manifest and signature beside the binaries, so a later
# re-verification does not depend on re-downloading them.
install -m 0644 "$WORK/SHA256SUM" "$WORK/SHA256SUM.sig" "$DEST/"

echo
echo "verified and installed: $DEST"
ls -1 "$DEST"
echo
# THESE ARE LINUX ELF BINARIES, for GNU/Linux with glibc 2.34+. There is no
# macOS build, and the failure on a Mac is the unhelpful "exec format error"
# rather than anything about platforms -- so say it here, where the files are
# still fresh in mind, instead of leaving it to be rediscovered.
if [ "$(uname -s)" = "Darwin" ]; then
  echo "NOTE: these are Linux binaries (ELF, glibc 2.34+) and cannot run on macOS."
  echo "      gen-cedar-policy.sh --docker runs them in a Linux container for you;"
  echo "      otherwise run the generator on a Linux host or a Cloud Desktop."
  echo
fi
echo "add it to PATH for this shell:"
echo "  export PATH=\"$DEST:\$PATH\""
echo
echo "then generate a policy:"
echo "  scripts/nitro/gen-cedar-policy.sh --app <application.yaml> --component <name> --profile confidential"
