#!/usr/bin/env bash
# Create the AWS KMS key that releases an image-layer key ONLY to an attested
# Nitro Sandbox.
#
# This is the step that turns confidential mode from "memory isolation" into
# confidentiality: without a key policy that checks the attestation
# measurements, the Nitro Secure Module's measurements are recorded and never
# verified. See examples/nitro-sandbox/README.md.
#
# THIS SCRIPT CREATES A BILLABLE AWS RESOURCE (a customer-managed KMS key, with
# a mandatory 7-30 day waiting period before deletion). It prints the plan and
# requires --yes to proceed. It creates and never modifies or deletes: if a key
# with the same alias exists it stops rather than repointing the alias, because
# repointing an alias silently changes which key every encrypted image needs.
#
# usage:
#   examples/nitro-sandbox/create-attested-key.sh \
#     --pcr17 <96 hex chars from eks-nitro-sandbox-genpolicy> \
#     --role-arn <the pod's EKS Pod Identity role ARN> \
#     [--build-principal <ARN that will run skopeo; default: the current caller>] \
#     [--alias alias/nitro-sandbox-attested-demo] \
#     [--region <region>] \
#     [--pcr0 <value>] \
#     --yes
set -euo pipefail

PCR17=""
PCR0=""
ROLE_ARN=""
BUILD_PRINCIPAL=""
ALIAS="alias/nitro-sandbox-attested-demo"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"
CONFIRM=0

while [ $# -gt 0 ]; do
  case "$1" in
    --pcr17)           PCR17="$2"; shift 2 ;;
    --pcr0)            PCR0="$2"; shift 2 ;;
    --role-arn)        ROLE_ARN="$2"; shift 2 ;;
    --build-principal) BUILD_PRINCIPAL="$2"; shift 2 ;;
    --alias)           ALIAS="$2"; shift 2 ;;
    --region)          REGION="$2"; shift 2 ;;
    --yes)             CONFIRM=1; shift ;;
    -h|--help)         sed -n '1,26p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

command -v aws >/dev/null     || { echo "missing: aws CLI" >&2; exit 1; }
command -v python3 >/dev/null || { echo "missing: python3" >&2; exit 1; }

[ -n "$PCR17" ]    || { echo "--pcr17 is required (eks-nitro-sandbox-genpolicy prints it)" >&2; exit 2; }
[ -n "$ROLE_ARN" ] || { echo "--role-arn is required (the pod's Pod Identity role)" >&2; exit 2; }
[ -n "$REGION" ]   || { echo "--region is required (or set AWS_REGION)" >&2; exit 2; }

# PCR17 is a SHA-384 measurement: 96 lowercase hex characters. Checking it here
# is worth the three lines -- a truncated or uppercase paste produces a key
# policy that is syntactically fine and silently never matches, and the only
# symptom is an AccessDeniedException on an image pull hours later.
if ! printf '%s' "$PCR17" | grep -qE '^[0-9a-fA-F]{96}$'; then
  echo "--pcr17 must be 96 hex characters (a SHA-384 measurement); got ${#PCR17}" >&2
  exit 2
fi
if [ -n "$PCR0" ] && ! printf '%s' "$PCR0" | grep -qE '^[0-9a-fA-F]{96}$'; then
  echo "--pcr0 must be 96 hex characters; got ${#PCR0}" >&2
  exit 2
fi

CALLER_JSON="$(aws sts get-caller-identity --output json)"
ACCOUNT_ID="$(printf '%s' "$CALLER_JSON" | python3 -c 'import json,sys;print(json.load(sys.stdin)["Account"])')"
CALLER_ARN="$(printf '%s' "$CALLER_JSON" | python3 -c 'import json,sys;print(json.load(sys.stdin)["Arn"])')"
BUILD_PRINCIPAL="${BUILD_PRINCIPAL:-$CALLER_ARN}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="$HERE/kms-key-policy.json"
[ -f "$TEMPLATE" ] || { echo "missing: $TEMPLATE" >&2; exit 1; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# Fill the template and strip the documentation fields. Comment and _notes are
# not IAM policy language: leaving them in gets MalformedPolicyDocumentException.
python3 - "$TEMPLATE" "$WORK/policy.json" <<PY
import json, sys

src, out = sys.argv[1], sys.argv[2]
policy = json.load(open(src))
policy.pop('_notes', None)

subs = {
    'ACCOUNT_ID': "$ACCOUNT_ID",
    'BUILD_PRINCIPAL_ARN': "$BUILD_PRINCIPAL",
    'POD_ROLE_ARN': "$ROLE_ARN",
    'PCR17_VALUE': "$PCR17".lower(),
}


def fill(x):
    if isinstance(x, str):
        for k, v in subs.items():
            x = x.replace(k, v)
        return x
    if isinstance(x, list):
        return [fill(i) for i in x]
    if isinstance(x, dict):
        return {k: fill(v) for k, v in x.items() if k != 'Comment'}
    return x


policy = fill(policy)

pcr0 = "$PCR0".lower()
if pcr0:
    for st in policy['Statement']:
        if st.get('Sid') == 'AllowDecryptOnlyFromAnAttestedSandbox':
            st['Condition']['StringEqualsIgnoreCase']['kms:RecipientAttestation:PCR0'] = pcr0

left = [k for k in subs if json.dumps(policy).find(k) >= 0]
if left:
    sys.exit('placeholders left unsubstituted: %s' % ', '.join(left))

json.dump(policy, open(out, 'w'), indent=2)
PY

echo "about to create, in account $ACCOUNT_ID, region $REGION:"
echo
echo "  a customer-managed KMS key (SYMMETRIC_DEFAULT, ENCRYPT_DECRYPT)"
echo "  alias:            $ALIAS"
echo "  key administrator: arn:aws:iam::$ACCOUNT_ID:root"
echo "  may ENCRYPT:      $BUILD_PRINCIPAL"
echo "  may DECRYPT:      $ROLE_ARN, and only when the attestation says"
echo "                      PCR17 = $(printf '%s' "$PCR17" | tr 'A-F' 'a-f')"
[ -n "$PCR0" ] && echo "                      PCR0  = $(printf '%s' "$PCR0" | tr 'A-F' 'a-f')"
echo "                      PCR8:ServiceName = eks.amazonaws.com"
echo
echo "the key policy that will be applied:"
sed 's/^/  /' "$WORK/policy.json"
echo

if [ "$CONFIRM" -ne 1 ]; then
  echo "re-run with --yes to create it. A KMS key is billable and cannot be deleted" >&2
  echo "immediately -- deletion schedules a 7-30 day waiting period." >&2
  exit 1
fi

# Fail rather than repoint: an alias that already exists points at a key some
# encrypted image somewhere needs, and moving it silently breaks that image's
# decryption with no error at move time.
if aws kms describe-key --key-id "$ALIAS" --region "$REGION" >/dev/null 2>&1; then
  echo "$ALIAS already exists in $REGION. Refusing to repoint it: every image" >&2
  echo "encrypted to the current key would stop decrypting. Pass a different" >&2
  echo "--alias, or update the existing key's policy instead:" >&2
  echo "  aws kms put-key-policy --key-id $ALIAS --policy-name default \\" >&2
  echo "    --region $REGION --policy file://<policy.json>" >&2
  exit 1
fi

KEY_ID="$(aws kms create-key \
  --region "$REGION" \
  --description "Nitro Sandbox attested image-layer key (PCR17-gated)" \
  --key-usage ENCRYPT_DECRYPT \
  --key-spec SYMMETRIC_DEFAULT \
  --policy "file://$WORK/policy.json" \
  --tags TagKey=managed-by,TagValue=open-agentic-platform \
         TagKey=purpose,TagValue=nitro-sandbox-attestation \
  --output json | python3 -c 'import json,sys;print(json.load(sys.stdin)["KeyMetadata"]["KeyId"])')"

aws kms create-alias --region "$REGION" --alias-name "$ALIAS" --target-key-id "$KEY_ID"

KEY_ARN="arn:aws:kms:$REGION:$ACCOUNT_ID:key/$KEY_ID"
echo
echo "created: $KEY_ARN  ($ALIAS)"
echo
echo "next, encrypt an image to it -- the FULL ARN is required, aliases are rejected:"
echo "  examples/nitro-sandbox/encrypt-image.sh \\"
echo "    --source public.ecr.aws/amazonlinux/amazonlinux:2023 \\"
echo "    --dest <your-registry>/attested-demo:encrypted \\"
echo "    --key-arn $KEY_ARN"
echo
echo "and make sure $ROLE_ARN is attached to the pod's ServiceAccount via EKS Pod"
echo "Identity. IRSA is NOT used for image decryption, and IMDS inside a sandbox"
echo "serves only placement metadata -- there are no IAM credentials there."
