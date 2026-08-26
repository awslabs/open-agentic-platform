#!/usr/bin/env bash
# bootstrap-agentspace.sh — idempotently reconcile the AWS Security Agent
# Agent Space + Application, then publish their IDs into a Kubernetes Secret the
# df-run security step reads. Runs as an ArgoCD PreSync hook Job on every sync.
#
# WHY a Job (not Terraform/ACK): the Security Agent space/application are preview
# resources with NO Terraform resource or ACK controller yet. The reconcile-Job
# pattern (find-or-create by name, write result to a Secret) is the standard
# GitOps shape for "an AWS object with no CRD" — same as langfuse-minio-init.
# Everything here is idempotent: re-running adopts the existing space/app rather
# than duplicating, so repeated ArgoCD syncs are safe no-ops.
#
# Env (from the Job spec):
#   AWS_REGION            region the agent space lives in
#   SPACE_NAME            agent space name to find-or-create (e.g. dark-factory)
#   SERVICE_ROLE_ARN      Security Agent service role (create-code-review --service-role)
#   DIFF_BUCKET           S3 bucket holding per-run diffs (registered on the space)
#   IDC_INSTANCE_ARN      IAM Identity Center instance ARN (create-application)
#   SECRET_NAME           Secret to write (agentSpaceId, applicationId, ...)
#   SECRET_NAMESPACE      namespace for that Secret
set -euo pipefail

log() { echo "[bootstrap] $*"; }

: "${AWS_REGION:?}" "${SPACE_NAME:?}" "${SERVICE_ROLE_ARN:?}" "${DIFF_BUCKET:?}" "${SECRET_NAME:?}" "${SECRET_NAMESPACE:?}"

# ── 1. Find-or-create the agent space (by name) ──────────────────────────────
log "reconciling agent space '${SPACE_NAME}' in ${AWS_REGION}..."
SPACE_ID="$(aws securityagent list-agent-spaces --region "$AWS_REGION" \
  --query "agentSpaceSummaries[?name=='${SPACE_NAME}'].agentSpaceId | [0]" \
  --output text 2>/dev/null || echo "")"

if [ -z "$SPACE_ID" ] || [ "$SPACE_ID" = "None" ]; then
  log "no existing space — creating..."
  SPACE_ID="$(aws securityagent create-agent-space --region "$AWS_REGION" \
    --name "$SPACE_NAME" \
    --description "Dark Factory autonomous coding pipeline — headless code security review on PR diffs" \
    --code-review-settings '{"controlsScanning":true,"generalPurposeScanning":true}' \
    --query 'agentSpaceId' --output text)"
  log "created agent space ${SPACE_ID}"
else
  log "adopting existing agent space ${SPACE_ID}"
fi

# ── 2. Register the service role + diff bucket on the space (idempotent) ──────
# update-agent-space is a FULL REPLACE (requires --name), so we always send the
# complete desired state. This is what lets create-code-review --service-role
# succeed ("... not found in agent instance IAM roles" otherwise).
log "registering service role + diff bucket on the space..."
aws securityagent update-agent-space --region "$AWS_REGION" \
  --agent-space-id "$SPACE_ID" \
  --name "$SPACE_NAME" \
  --description "Dark Factory autonomous coding pipeline — headless code security review on PR diffs" \
  --code-review-settings '{"controlsScanning":true,"generalPurposeScanning":true}' \
  --aws-resources "{\"iamRoles\":[\"${SERVICE_ROLE_ARN}\"],\"s3Buckets\":[\"arn:aws:s3:::${DIFF_BUCKET}\"]}" \
  >/dev/null
log "space resources registered."

# ── 3. Find-or-create the Application (so the CONSOLE renders the space) ──────
# The console keys its entire view off the account-level Application. Without it,
# the console shows "AWS Security Agent application hasn't been created" even
# though reviews run server-side. Needs an IAM Identity Center instance.
APP_ID="$(aws securityagent list-applications --region "$AWS_REGION" \
  --query 'applicationSummaries[0].applicationId | [0]' --output text 2>/dev/null || echo "")"

if [ -z "$APP_ID" ] || [ "$APP_ID" = "None" ]; then
  if [ -n "${IDC_INSTANCE_ARN:-}" ]; then
    log "creating Application (IDC-backed) so the console renders the space..."
    # Only ONE application per account is allowed. create-application fails with
    # ServiceQuotaExceededException if one already exists but list-applications
    # didn't surface it (eventual consistency / paging). Treat "already exists" as
    # success and re-list to adopt it — idempotent, never fatal.
    set +e
    CREATE_OUT="$(aws securityagent create-application --region "$AWS_REGION" \
      --idc-instance-arn "$IDC_INSTANCE_ARN" \
      --role-arn "$SERVICE_ROLE_ARN" \
      --query 'applicationId' --output text 2>&1)"
    CREATE_RC=$?
    set -e
    if [ "$CREATE_RC" -eq 0 ]; then
      APP_ID="$CREATE_OUT"
      log "created application ${APP_ID}"
    elif echo "$CREATE_OUT" | grep -qiE "already exists|ServiceQuotaExceeded"; then
      log "application already exists — adopting it."
      APP_ID="$(aws securityagent list-applications --region "$AWS_REGION" \
        --query 'applicationSummaries[0].applicationId' --output text 2>/dev/null || echo "")"
      if [ -z "$APP_ID" ] || [ "$APP_ID" = "None" ]; then APP_ID="existing"; fi
      log "application id: ${APP_ID}"
    else
      log "WARN: create-application failed (non-fatal): ${CREATE_OUT}"
      APP_ID=""
    fi
  else
    log "WARN: no IDC_INSTANCE_ARN provided — skipping Application creation."
    log "WARN: the API/reviews still work, but the console will show 'application hasn't been created'."
    APP_ID=""
  fi
else
  log "adopting existing application ${APP_ID}"
fi

# ── 4. Publish IDs into the Secret the df-run security step reads ─────────────
log "writing Secret ${SECRET_NAMESPACE}/${SECRET_NAME}..."
kubectl create secret generic "$SECRET_NAME" -n "$SECRET_NAMESPACE" \
  --from-literal=agentSpaceId="$SPACE_ID" \
  --from-literal=applicationId="$APP_ID" \
  --from-literal=serviceRoleArn="$SERVICE_ROLE_ARN" \
  --from-literal=diffBucket="$DIFF_BUCKET" \
  --from-literal=region="$AWS_REGION" \
  --dry-run=client -o yaml | kubectl apply -f -

log "done. agentSpaceId=${SPACE_ID} applicationId=${APP_ID:-<none>}"
