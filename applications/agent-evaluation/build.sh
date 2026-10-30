#!/usr/bin/env bash
# Build and push the agent-evaluation reconciler image.
#
# Multi-arch is required, not optional: OAP clusters run mixed arm64 and amd64
# nodes, so a single-arch image schedules onto half the fleet and ImagePulls on
# the rest.
#
# Usage:
#   ./build.sh                                  # build only, no push
#   ./build.sh --push                           # build + push to $REGISTRY
#   REGISTRY=public.ecr.aws/z0a4o2j5 ./build.sh --push
#   VERSION=0.1.2 ./build.sh --push
#
# Registries:
#   public.ecr.aws/z0a4o2j5   the platform's published images (browser-mcp,
#                             strands-agent). Needs credentials for the account
#                             that owns that alias -- see README, "Publishing".
#   <acct>.dkr.ecr.<region>.amazonaws.com   a private repo, for testing in your
#                             own account before publishing.
set -euo pipefail

VERSION="${VERSION:-0.1.1}"
REGISTRY="${REGISTRY:-public.ecr.aws/z0a4o2j5}"
IMAGE="agent-evaluation"
PLATFORMS=("linux/arm64" "linux/amd64")
PUSH=false
[[ "${1:-}" == "--push" ]] && PUSH=true

CLI="${CONTAINER_CLI:-podman}"
command -v "$CLI" >/dev/null || { echo "error: $CLI not found" >&2; exit 1; }

cd "$(dirname "${BASH_SOURCE[0]}")"

LOCAL="${IMAGE}:${VERSION}"
echo "==> building ${LOCAL} for ${PLATFORMS[*]}"

# One manifest list, built up one platform at a time. `podman build --manifest`
# appends to the list rather than replacing it, so the list has to be cleared
# first or a rebuild accumulates stale entries.
"$CLI" manifest rm "$LOCAL" 2>/dev/null || true
"$CLI" manifest create "$LOCAL" >/dev/null

for platform in "${PLATFORMS[@]}"; do
  echo "    $platform"
  "$CLI" build --platform "$platform" --manifest "$LOCAL" . >/dev/null
done

echo "==> manifest list:"
"$CLI" manifest inspect "$LOCAL" |
  grep -E '"architecture"|"os"' | paste - - | sed 's/^/    /'

if [[ "$PUSH" != true ]]; then
  echo "==> built ${LOCAL} (not pushed; pass --push)"
  exit 0
fi

TARGET="${REGISTRY}/${IMAGE}:${VERSION}"
echo "==> pushing ${TARGET}"
# --all pushes every architecture in the list, not just the host's. Without it
# the remote tag is a manifest list with one entry.
"$CLI" manifest push --all "$LOCAL" "docker://${TARGET}"

echo "==> pushed ${TARGET}"
echo "    pin by digest in production:"
"$CLI" manifest inspect "$LOCAL" | grep -m1 '"digest"' | sed 's/^/    /' || true
