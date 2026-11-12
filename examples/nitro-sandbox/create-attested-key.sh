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
# NORMALISE AN ASSUMED-ROLE SESSION TO ITS ROLE. sts:get-caller-identity returns
#   arn:aws:sts::<acct>:assumed-role/<role>/<session>
# for anyone using Isengard, SSO or a CI role -- i.e. almost everyone -- and a KMS
# key policy rejects a session ARN as a principal. Left as-is, the default
# produced a MalformedPolicyDocumentException AFTER the plan had been printed and
# confirmed. The role ARN is what the policy needs, and it is what the session is
# a session of.
if [ -z "${BUILD_PRINCIPAL:-}" ]; then
  case "$CALLER_ARN" in
    arn:aws:sts::*:assumed-role/*)
      _role="${CALLER_ARN#*:assumed-role/}"; _role="${_role%%/*}"
      BUILD_PRINCIPAL="arn:aws:iam::$ACCOUNT_ID:role/$_role"
      echo "note: caller is an assumed-role session; using the role ARN as the build" >&2
      echo "      principal ($BUILD_PRINCIPAL). A key policy cannot name a session." >&2
      ;;
    *) BUILD_PRINCIPAL="$CALLER_ARN" ;;
  esac
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="$HERE/kms-key-policy.json"
[ -f "$TEMPLATE" ] || { echo "missing: $TEMPLATE" >&2; exit 1; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# Fill the template and strip the documentation fields. Comment and _notes are
# not IAM policy language: leaving them in gets MalformedPolicyDocumentException.
# <<'PY' -- QUOTED, and every value arrives through argv. With an unquoted
# heredoc the shell expands these into the Python SOURCE, so an ARN or alias
# containing a quote, a backslash or a brace is a syntax error or worse, and the
# values are shell-visible rather than data.
python3 - "$TEMPLATE" "$WORK/policy.json" \
  "$ACCOUNT_ID" "$BUILD_PRINCIPAL" "$ROLE_ARN" "$PCR17" "$PCR0" <<'PY'
import json, sys

src, out, account, build, role, pcr17, pcr0 = sys.argv[1:8]
policy = json.load(open(src))
policy.pop('_notes', None)

subs = {
    'ACCOUNT_ID': account,
    'BUILD_PRINCIPAL_ARN': build,
    'POD_ROLE_ARN': role,
    # Substituted in BOTH the attested Allow and the unattested Deny, which is
    # the point of doing it here rather than by hand: the two must agree or the
    # key either blocks the sandbox or stops being gated at all.
    'PCR17_VALUE': pcr17.lower(),
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

# PCR0 pins the guest system image, and belongs only on the Allow: the Deny is
# the catch-all for callers presenting no attestation, and adding a second
# negated key there would weaken it (the keys in one condition block are ANDed,
# so the Deny would need BOTH to mismatch).
pcr0 = pcr0.lower()
if pcr0:
    for st in policy['Statement']:
        if st.get('Sid') == 'AllowDecryptOnlyFromAnAttestedSandbox':
            st['Condition']['StringEqualsIgnoreCase']['kms:RecipientAttestation:PCR0'] = pcr0

left = [k for k in subs if json.dumps(policy).find(k) >= 0]
if left:
    sys.exit('placeholders left unsubstituted: %s' % ', '.join(left))

# The Allow and the Deny must BOTH carry the measurement. If a future edit drops
# one, the key either refuses the sandbox it was made for or stops being gated,
# and neither shows up until an image pull fails (or does not) in production.
n = json.dumps(policy).count(pcr17.lower())
if n != 2:
    sys.exit('expected PCR17 in exactly 2 statements (the attested Allow and the '
             'unattested Deny); found %d. Refusing to create a key whose gate is '
             'not symmetric -- check kms-key-policy.json.' % n)
sids = {st.get('Sid') for st in policy['Statement']}
if 'DenyDataPlaneWithoutAttestation' not in sids:
    sys.exit('kms-key-policy.json has no DenyDataPlaneWithoutAttestation statement. '
             'Without it, key administration can delegate kms:Decrypt through IAM '
             'with no attestation, which is the boundary this key exists to enforce.')

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
echo "  may NOT decrypt:  everyone else, INCLUDING key administrators and anyone"
echo "                      they delegate to through IAM -- an explicit Deny on"
echo "                      the data-plane actions, which no Allow can override"
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
# THREE outcomes, not two. `>/dev/null 2>&1` collapsed "the alias is not there"
# together with "I could not tell" -- a throttle, expired credentials, or a role
# without kms:DescribeKey -- and then created a key anyway. Only a genuine
# NotFoundException is permission to proceed.
set +e
DESCRIBE_ERR="$(aws kms describe-key --key-id "$ALIAS" --region "$REGION" 2>&1 >/dev/null)"
DESCRIBE_RC=$?
set -e

if [ "$DESCRIBE_RC" -eq 0 ]; then
  echo "$ALIAS already exists in $REGION. Refusing to repoint it: every image" >&2
  echo "encrypted to the current key would stop decrypting. Pass a different" >&2
  echo "--alias, or update the existing key's policy instead:" >&2
  echo "  aws kms put-key-policy --key-id $ALIAS --policy-name default \\" >&2
  echo "    --region $REGION --policy file://<policy.json>" >&2
  exit 1
elif ! printf '%s' "$DESCRIBE_ERR" | grep -q 'NotFoundException'; then
  echo "cannot determine whether $ALIAS exists in $REGION:" >&2
  echo "  $DESCRIBE_ERR" >&2
  echo >&2
  echo "Refusing to create a key on a guess. Creating one here would be billable" >&2
  echo "and undeletable for 7-30 days, and if the alias DOES exist the follow-up" >&2
  echo "create-alias would fail and leave it orphaned. Fix the error above (often" >&2
  echo "kms:DescribeKey on the caller, or expired credentials) and re-run." >&2
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

KEY_ARN="arn:aws:kms:$REGION:$ACCOUNT_ID:key/$KEY_ID"

# The key EXISTS now and is billable. If the alias fails, say so with the ARN and
# the exact cleanup command: a key nobody has the id for cannot be deleted, and
# it cannot be deleted quickly even when it can be found.
if ! aws kms create-alias --region "$REGION" --alias-name "$ALIAS" --target-key-id "$KEY_ID"; then
  echo >&2
  echo "THE KEY WAS CREATED BUT THE ALIAS WAS NOT. Do not re-run this script -- it" >&2
  echo "would create a SECOND billable key. The key is:" >&2
  echo >&2
  echo "  $KEY_ARN" >&2
  echo >&2
  echo "Either attach the alias yourself:" >&2
  echo "  aws kms create-alias --region $REGION --alias-name $ALIAS --target-key-id $KEY_ID" >&2
  echo >&2
  echo "or schedule it for deletion (7-day minimum waiting period):" >&2
  echo "  aws kms schedule-key-deletion --region $REGION --key-id $KEY_ID --pending-window-in-days 7" >&2
  exit 1
fi

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
