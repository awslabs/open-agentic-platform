#!/usr/bin/env bash
# Generate the Cedar policy for a sandboxed OAP component, and print the PCR17
# value a KMS key policy has to pin.
#
# WHY THIS SCRIPT EXISTS. eks-nitro-sandbox-genpolicy takes a POD MANIFEST. An
# OAP developer writes an OAM Application, and the pod spec that eventually runs
# is rendered from CUE by KubeVela, wrapped in an Argo Rollout or a
# SandboxTemplate, and then possibly mutated by admission webhooks. Handing
# genpolicy the Application does nothing useful. This extracts the real pod
# template and feeds it the thing it wants.
#
# A confidential policy pins container admission fields -- image digest, args,
# environment variables, mounts, capabilities, security settings -- so it has to
# be generated from the FINAL spec and regenerated whenever any of them changes.
# A new image digest alone is enough. A stale policy does not degrade quietly:
# the pod is DENIED. Put this in the pipeline that produces your manifests.
#
# TWO SOURCES, AND THE DIFFERENCE MATTERS
#
#   --from rollout/<name>          Read the pod template from the LIVE object.
#   --from sandboxtemplate/<name>  Post-webhook, so this is the spec that
#                                  actually runs. Prefer it whenever the
#                                  Application is already deployed.
#   --app <file> --component <x>   Render the Application offline (helm template
#                                  + vela dry-run). Right for a CI job that must
#                                  produce the policy BEFORE anything deploys --
#                                  but it cannot see mutating webhooks, so if
#                                  your cluster injects sidecars or rewrites pod
#                                  specs, verify against the live object before
#                                  trusting it.
#
# usage:
#   scripts/nitro/gen-cedar-policy.sh --from rollout/my-agent [options]
#   scripts/nitro/gen-cedar-policy.sh --app app.yaml --component my-agent [options]
#
# options:
#   --profile <isolated|confidential|path.yaml>  genpolicy profile (default confidential)
#   --namespace <ns>                             namespace for --from (default default)
#   --out <file>                                 where to write the base64 policy
#   --genpolicy <path>                           eks-nitro-sandbox-genpolicy binary
#   --docker                                     run the generator in a Linux container
#                                                (required on macOS: the binary is Linux ELF)
#   --                                           pass everything after this to genpolicy,
#                                                e.g. -- --deny-exec --require-any-signer <arn>
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
FROM=""
APP=""
COMPONENT=""
PROFILE="confidential"
NAMESPACE="default"
OUT=""
GENPOLICY=""
USE_DOCKER=0
EXTRA=()

while [ $# -gt 0 ]; do
  case "$1" in
    --from)      FROM="$2"; shift 2 ;;
    --app)       APP="$2"; shift 2 ;;
    --component) COMPONENT="$2"; shift 2 ;;
    --profile)   PROFILE="$2"; shift 2 ;;
    --namespace|-n) NAMESPACE="$2"; shift 2 ;;
    --out)       OUT="$2"; shift 2 ;;
    --genpolicy) GENPOLICY="$2"; shift 2 ;;
    --docker)    USE_DOCKER=1; shift ;;
    --)          shift; EXTRA=("$@"); break ;;
    -h|--help)   sed -n '1,46p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [ -z "$FROM" ] && { [ -z "$APP" ] || [ -z "$COMPONENT" ]; }; then
  echo "need either --from <kind>/<name>, or --app <file> --component <name>" >&2
  exit 2
fi

command -v python3 >/dev/null || { echo "missing: python3" >&2; exit 1; }
python3 -c 'import yaml' 2>/dev/null || { echo "missing: python3 PyYAML (pip install pyyaml)" >&2; exit 1; }

# Locate the generator. The download script installs it under .platform/private,
# keyed by architecture, so look there before PATH.
if [ -z "$GENPOLICY" ]; then
  case "$(uname -m)" in
    x86_64|amd64)  A=x86_64 ;;
    arm64|aarch64) A=aarch64 ;;
    *) A="" ;;
  esac
  if [ -n "$A" ] && [ -x "$REPO_ROOT/.platform/private/nitro-sandbox-runtime/$A/eks-nitro-sandbox-genpolicy" ]; then
    GENPOLICY="$REPO_ROOT/.platform/private/nitro-sandbox-runtime/$A/eks-nitro-sandbox-genpolicy"
  else
    GENPOLICY="$(command -v eks-nitro-sandbox-genpolicy || true)"
  fi
fi
if [ -z "$GENPOLICY" ] || [ ! -x "$GENPOLICY" ]; then
  echo "eks-nitro-sandbox-genpolicy not found. Get it with:" >&2
  echo "  scripts/nitro/download-runtime-binaries.sh" >&2
  exit 1
fi

# The published generator is a Linux ELF binary. On macOS it cannot run
# natively, and the error ('exec format error') says nothing about why -- so
# refuse early with the fix rather than letting it fail mid-pipeline.
#
# The test is on the BINARY, not on the OS: anything that is not an ELF (a
# wrapper script, a native build) is allowed to run wherever we are. That keeps
# the check from blocking a legitimate setup just because the host is not Linux.
if [ "$USE_DOCKER" -eq 0 ] && [ "$(uname -s)" != "Linux" ] &&
   [ "$(head -c 4 "$GENPOLICY" | od -An -c | tr -d ' \n')" = "177ELF" ]; then
  echo "$GENPOLICY is a Linux ELF binary and this is $(uname -s)." >&2
  echo "Re-run with --docker to execute it in a Linux container, or run this on a Linux host." >&2
  exit 1
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# ---------------------------------------------------------------- pod template
if [ -n "$FROM" ]; then
  command -v kubectl >/dev/null || { echo "missing: kubectl" >&2; exit 1; }
  KIND="${FROM%%/*}"
  NAME="${FROM##*/}"
  COMPONENT="${COMPONENT:-$NAME}"
  kubectl get "$KIND" "$NAME" -n "$NAMESPACE" -o yaml > "$WORK/object.yaml"
  SRC="$WORK/object.yaml"
  MODE=object
else
  command -v helm >/dev/null || { echo "missing: helm" >&2; exit 1; }
  command -v vela >/dev/null || { echo "missing: vela" >&2; exit 1; }
  # Render the definitions exactly as the platform does -- Helm substitutes the
  # globals INTO the CUE before KubeVela ever parses it, so a render that skips
  # Helm produces a pod spec with unsubstituted placeholders in it.
  mkdir -p "$WORK/chart" "$WORK/defs"
  cp -R "$REPO_ROOT/gitops/addons/charts/oam-agent-components/." "$WORK/chart/"
  helm template oap-genpolicy "$WORK/chart" > "$WORK/chart-rendered.yaml"
  python3 - "$WORK/chart-rendered.yaml" "$WORK/defs" <<'PY'
import sys, yaml, os
for d in yaml.safe_load_all(open(sys.argv[1])):
    if d and d.get('kind') in ('ComponentDefinition', 'TraitDefinition'):
        yaml.safe_dump(d, open(os.path.join(sys.argv[2], '%s-%s.yaml' % (d['kind'], d['metadata']['name'])), 'w'),
                       default_flow_style=False, width=100000)
PY
  vela dry-run --offline -d "$WORK/defs" -f "$APP" > "$WORK/rendered.yaml"
  SRC="$WORK/rendered.yaml"
  MODE=render
fi

# Extract the pod template and re-wrap it as a standalone Pod, which is what
# genpolicy takes. The two shapes OAP produces live in different places:
# a Rollout keeps it at .spec.template, a SandboxTemplate at .spec.podTemplate.
python3 - "$SRC" "$MODE" "$COMPONENT" "$WORK/pod.yaml" <<'PY'
import sys, yaml

src, mode, component, out = sys.argv[1:5]
docs = [d for d in yaml.safe_load_all(open(src)) if d]

POD_TEMPLATE_AT = {
    'Rollout': ('spec', 'template'),
    'Deployment': ('spec', 'template'),
    'StatefulSet': ('spec', 'template'),
    'SandboxTemplate': ('spec', 'podTemplate'),
}


def dig(d, path):
    for k in path:
        if not isinstance(d, dict) or k not in d:
            return None
        d = d[k]
    return d


cands = []
for d in docs:
    path = POD_TEMPLATE_AT.get(d.get('kind'))
    if not path:
        continue
    tpl = dig(d, path)
    if not tpl:
        continue
    name = (d.get('metadata') or {}).get('name', '')
    cands.append((d['kind'], name, tpl))

if mode == 'render':
    # A rendered Application carries every component. Match on the object name,
    # which the OAP convention makes equal to the component name -- except the
    # sandbox component's template, which is <component>-template.
    want = [c for c in cands if c[1] in (component, component + '-template')]
    if not want:
        sys.exit('no pod template found for component %r. Objects seen: %s' %
                 (component, ', '.join('%s/%s' % (k, n) for k, n, _ in cands) or 'none'))
    if len(want) > 1:
        # An agent with isolation: session renders both. Prefer the SandboxTemplate,
        # since that is the per-session instance a confidential policy is for;
        # name the other explicitly to get it.
        want.sort(key=lambda c: 0 if c[0] == 'SandboxTemplate' else 1)
    kind, name, tpl = want[0]
else:
    if not cands:
        sys.exit('no pod template in %s' % src)
    kind, name, tpl = cands[0]

meta = dict(tpl.get('metadata') or {})
annotations = dict(meta.get('annotations') or {})
# Drop any policy already attached. Leaving it would feed the previous
# generation's output back in; harmless for the pinning (which covers container
# fields, not annotations) but it makes the diff between two runs unreadable.
annotations.pop('sandbox.amazonaws.com/policy', None)

pod = {
    'apiVersion': 'v1',
    'kind': 'Pod',
    'metadata': {
        'name': name,
        **({'labels': meta['labels']} if meta.get('labels') else {}),
        **({'annotations': annotations} if annotations else {}),
    },
    'spec': tpl['spec'],
}
yaml.safe_dump(pod, open(out, 'w'), default_flow_style=False, width=100000)
print('%s/%s' % (kind, name))
PY

echo "==> pod manifest: $WORK/pod.yaml"

# ---------------------------------------------------------------- generate
# genpolicy resolves image digests and image config from the REGISTRY, so this
# step needs network and, for a private registry, credentials. --skip-image-fetch
# exists for an offline run but leaves image-derived fields unpinned, which is
# most of the point in confidential mode.
PROFILE_ARG="$PROFILE"
if [ -f "$PROFILE" ]; then
  cp "$PROFILE" "$WORK/profile.yaml"
  PROFILE_ARG="./profile.yaml"
fi

set +e
if [ "$USE_DOCKER" -eq 1 ]; then
  command -v docker >/dev/null || { echo "missing: docker (needed for --docker)" >&2; exit 1; }
  cp "$GENPOLICY" "$WORK/genpolicy"
  chmod +x "$WORK/genpolicy"
  # AL2023 for glibc 2.34+, which the binaries require. No --platform: the
  # container has to match the binary's architecture, and the default (the
  # host's) is what the download script selected.
  docker run --rm -v "$WORK:/w" -w /w public.ecr.aws/amazonlinux/amazonlinux:2023 \
    ./genpolicy --profile "$PROFILE_ARG" "${EXTRA[@]}" pod.yaml \
    > "$WORK/out.txt" 2> "$WORK/err.txt"
else
  ( cd "$WORK" && "$GENPOLICY" --profile "$PROFILE_ARG" "${EXTRA[@]}" pod.yaml ) \
    > "$WORK/out.txt" 2> "$WORK/err.txt"
fi
RC=$?
set -e

if [ "$RC" -ne 0 ]; then
  echo "genpolicy failed (exit $RC):" >&2
  cat "$WORK/err.txt" >&2
  exit "$RC"
fi

# The generator prints the base64 policy and the predicted PCR17. Parse rather
# than assume which stream each lands on: the policy is the long base64 token,
# PCR17 is 96 hex characters (SHA-384).
POLICY="$(python3 - "$WORK/out.txt" <<'PY'
import re, sys
best = ''
for line in open(sys.argv[1]):
    t = line.strip()
    if len(t) > 100 and re.fullmatch(r'[A-Za-z0-9+/=]+', t) and len(t) > len(best):
        best = t
print(best)
PY
)"
PCR17="$(cat "$WORK/out.txt" "$WORK/err.txt" | grep -oE '\b[0-9a-f]{96}\b' | head -1 || true)"

if [ -z "$POLICY" ]; then
  echo "could not find a base64 policy in the generator output. Raw output:" >&2
  cat "$WORK/out.txt" "$WORK/err.txt" >&2
  exit 1
fi

OUT="${OUT:-$COMPONENT-cedar-policy.b64}"
printf '%s\n' "$POLICY" > "$OUT"

echo
echo "policy written: $OUT  ($(printf '%s' "$POLICY" | wc -c | tr -d ' ') base64 chars)"
echo
echo "1. put it in the Application:"
echo "     cedarPolicy: \"$(printf '%s' "$POLICY" | cut -c1-24)...\"   # contents of $OUT"
echo
if [ -n "$PCR17" ]; then
  echo "2. pin it on the KMS key policy. PCR17 (the measurement of THIS policy):"
  echo
  echo "     \"Condition\": {"
  echo "       \"StringEqualsIgnoreCase\": {"
  echo "         \"kms:RecipientAttestation:PCR17\": \"$PCR17\","
  echo "         \"kms:RecipientAttestation:PCR8:ServiceName\": \"eks.amazonaws.com\""
  echo "       }"
  echo "     }"
  echo
  echo "   PCR8:ServiceName is NOT optional alongside PCR17. PCR17 alone would"
  echo "   accept the same policy hash presented from a guest image AWS did not"
  echo "   sign; the ServiceName form keeps matching when AWS rotates the signing"
  echo "   certificate, which changes the raw PCR8 hash but not the identity."
else
  echo "2. the generator printed no PCR17 value. Its raw output:"
  cat "$WORK/out.txt" "$WORK/err.txt"
fi
echo
echo "REGENERATE this whenever anything the policy pins changes -- image digest,"
echo "args, env, mounts, capabilities, security settings -- and update the PCR17"
echo "condition in the same change. A stale policy means a DENIED pod, not a"
echo "degraded one."
