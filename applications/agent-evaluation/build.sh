#!/usr/bin/env bash
# Build and push the agent-evaluation reconciler image with docker buildx.
#
# Multi-arch is required, not optional: OAP clusters run mixed arm64 and amd64
# nodes, so a single-arch image schedules onto half the fleet and ImagePulls on
# the rest. buildx produces one image index covering every platform.
#
# Usage:
#   ./build.sh [--push] [--registry R] [--repository NAME] [--version V]
#              [--tag EXTRA_TAG]... [--platforms P1,P2] [--no-login]
#
#   ./build.sh                                  # build both arches, no push
#   ./build.sh --push                           # build + push to $REGISTRY
#   REGISTRY=<account>.dkr.ecr.<region>.amazonaws.com ./build.sh --push
#   ./build.sh --push --tag "$(git rev-parse --short HEAD)"
#
# Environment (flags win over env):
#   REGISTRY     registry host[/namespace]   default public.ecr.aws/z0a4o2j5
#   REPOSITORY   repository name             default agent-evaluation
#   VERSION      primary tag                 default 0.3.0 (see below)
#   EXTRA_TAGS   space-separated extra tags  default none
#   PLATFORMS    comma-separated platforms   default linux/amd64,linux/arm64
#   ECR_LOGIN    auto|false                  default auto
#   BUILDER      buildx builder name         default agent-evaluation-builder
#
# Registries:
#   public.ecr.aws/<alias>   the platform's published images. Needs credentials
#                            for the account that owns that alias.
#   <account>.dkr.ecr.<region>.amazonaws.com   a private repo, for testing in your
#                            own account before publishing.
# With ECR_LOGIN=auto and --push, the script logs docker in to either kind of
# ECR host using the current AWS credentials. Other registries are left alone:
# run `docker login` yourself.
set -euo pipefail

# THE IMAGE VERSION LINE. Keep in lockstep with exactly three things:
#   - image.tag in gitops/addons/charts/agent-evaluation/values.yaml
#   - appVersion in that chart's Chart.yaml
#   - version in this package's pyproject.toml
#
# NOT with the chart's own `version`, nor the registry entry's `defaultVersion`:
# those track the TEMPLATES and move independently. Conflating the two lines is
# how this drifted in the first place -- a build defaulting to a tag the chart
# does not pull publishes to a tag nothing references, and the addon sits in
# ImagePullBackOff looking like a registry or credentials problem.
VERSION="${VERSION:-0.3.0}"
REGISTRY="${REGISTRY:-public.ecr.aws/z0a4o2j5}"
REPOSITORY="${REPOSITORY:-agent-evaluation}"
PLATFORMS="${PLATFORMS:-linux/amd64,linux/arm64}"
ECR_LOGIN="${ECR_LOGIN:-auto}"
BUILDER="${BUILDER:-agent-evaluation-builder}"
read -r -a TAGS <<<"${EXTRA_TAGS:-}"
PUSH=false

die() { echo "error: $*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --push)       PUSH=true ;;
    --registry)   REGISTRY="${2:?--registry needs a value}"; shift ;;
    --repository) REPOSITORY="${2:?--repository needs a value}"; shift ;;
    --version)    VERSION="${2:?--version needs a value}"; shift ;;
    --tag)        TAGS+=("${2:?--tag needs a value}"); shift ;;
    --platforms)  PLATFORMS="${2:?--platforms needs a value}"; shift ;;
    --no-login)   ECR_LOGIN=false ;;
    -h|--help)    sed -n '2,33p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *)            die "unknown argument: $1 (see --help)" ;;
  esac
  shift
done

REGISTRY="${REGISTRY%/}"
command -v docker >/dev/null || die "docker not found"
docker buildx version >/dev/null 2>&1 || die "docker buildx plugin not available"
docker info >/dev/null 2>&1 || die "docker daemon not reachable (start Docker Desktop?)"

cd "$(dirname "${BASH_SOURCE[0]}")"

IMAGE="${REGISTRY}/${REPOSITORY}"
TAG_ARGS=(--tag "${IMAGE}:${VERSION}")
for t in "${TAGS[@]+"${TAGS[@]}"}"; do
  [[ -n "$t" ]] && TAG_ARGS+=(--tag "${IMAGE}:${t}")
done

# The aws CLI is needed only for an ECR host, so a push to any other registry
# (logged in with `docker login`) works without it.
need_aws() {
  command -v aws >/dev/null || die "aws CLI not found (needed for ECR login; pass --no-login to skip)"
}

ecr_login() {
  local host="${REGISTRY%%/*}" region
  if [[ "$host" =~ ^[0-9]{12}\.dkr\.ecr(-fips)?\.([a-z0-9-]+)\.amazonaws\.com(\.cn)?$ ]]; then
    need_aws
    region="${BASH_REMATCH[2]}"
    echo "==> logging in to ${host} (region ${region})"
    aws ecr get-login-password --region "$region" |
      docker login --username AWS --password-stdin "$host"
  elif [[ "$host" == "public.ecr.aws" ]]; then
    need_aws
    # ECR Public's auth endpoint only exists in us-east-1.
    echo "==> logging in to public.ecr.aws"
    aws ecr-public get-login-password --region us-east-1 |
      docker login --username AWS --password-stdin public.ecr.aws
  fi
}

if [[ "$PUSH" == true && "$ECR_LOGIN" == auto ]]; then
  ecr_login
fi

# The default docker driver cannot build an image index; a docker-container
# builder can, and emulates the non-native arch through QEMU/binfmt.
if ! docker buildx inspect "$BUILDER" >/dev/null 2>&1; then
  echo "==> creating buildx builder ${BUILDER}"
  docker buildx create --name "$BUILDER" --driver docker-container >/dev/null
fi

# A builder lists only the platforms it can run: the native one, plus each arch
# with a binfmt emulator registered. Without the emulator a cross build fails
# deep inside the first RUN step with "exec format error", so check up front.
builder_platforms="$(docker buildx inspect "$BUILDER" --bootstrap |
  sed -n 's/^Platforms:[[:space:]]*//p' | tr -d ' ' | tr ',' '\n')"
IFS=',' read -r -a requested <<<"$PLATFORMS"
for p in "${requested[@]}"; do
  p="${p// /}"
  if ! grep -qxF "$p" <<<"$builder_platforms"; then
    arch="${p#*/}"
    die "builder ${BUILDER} cannot build ${p} (no binfmt emulator for it); install one with: docker run --privileged --rm tonistiigi/binfmt --install ${arch%%/*}"
  fi
done

OUTPUT_ARGS=()
if [[ "$PUSH" == true ]]; then
  OUTPUT_ARGS=(--push)
else
  # A multi-platform result cannot be --load'ed into the local image store, so
  # a no-push run only proves every platform builds; results stay in the cache.
  echo "==> build only (pass --push to publish)"
fi

echo "==> building ${IMAGE}:${VERSION} for ${PLATFORMS}"
docker buildx build \
  --builder "$BUILDER" \
  --platform "$PLATFORMS" \
  --provenance=false \
  "${TAG_ARGS[@]}" \
  "${OUTPUT_ARGS[@]+"${OUTPUT_ARGS[@]}"}" \
  .

if [[ "$PUSH" == true ]]; then
  echo "==> pushed; image index:"
  docker buildx imagetools inspect "${IMAGE}:${VERSION}"
  echo "    pin by digest in production (the Digest line above)."
fi
