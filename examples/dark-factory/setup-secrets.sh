#!/usr/bin/env bash
# Create the three Dark Factory GitHub credentials in AWS Secrets Manager.
#
# The charts do NOT create these — external-secrets pulls them from Secrets
# Manager into the three namespaces that consume them. Until they exist, the
# ExternalSecrets sit in SecretSyncedError and the warm pods hang in
# ContainerCreating on `secret "dark-factory-github-coder" not found`.
#
# ─── STEP 1: mint three fine-grained PATs (manual — no API exists for this) ────
#
# GitHub ships no PAT-creation endpoint (POST /user/personal-access-tokens,
# /user/tokens, /authorizations all 404) and `gh auth` has no create subcommand,
# so this step cannot be scripted. A token able to mint tokens would be a
# privilege-escalation path.
#
#   https://github.com/settings/personal-access-tokens/new
#
# For EACH token: Resource owner = your org/user, Repository access =
# "Only select repositories" → the target repo. Metadata:read is added
# automatically. Do NOT grant Administration.
#
#   df-events        Contents          read
#                    Webhooks          write   (Argo Events self-registers the
#                                               repo webhook; a read-only token
#                                               fails registration)
#
#   df-orchestrator  Contents          write   (PUT /pulls/{n}/merge is gated on
#                                               Contents for fine-grained PATs,
#                                               + DELETE of the merged branch ref)
#                    Pull requests     write   (the merge itself)
#                    Commit statuses   write   (holdout/security/devops verdicts)
#                    Issues            write   (sticky status comment — PR
#                                               comments are issue comments)
#
#   df-coder         Contents          write   (push df/issue-<n>)
#                    Pull requests     write   (the coder opens its OWN PR from
#                                               inside the Kata VM)
#                    Commit statuses   write   (self-reports
#                                               dark-factory/implementation, the
#                                               signal df-run polls for)
#                    Issues            read    (GET /issues/{n} → SPEC.md)
#
# These are the MINIMUM sets the code exercises, verified against
# examples/dark-factory/coder/entrypoint.js and the chart's review/*.js.
# Under-scoping fails MID-RUN — after a sandbox is claimed and model tokens are
# spent. See docs/dark-factory/README.md §10a.
#
# ⚠️ The coder token holds Contents:write, and fine-grained PATs gate the merge
# endpoint on Contents — so it CAN merge its own PR. Branch protection (or a
# ruleset) requiring the dark-factory/* checks on the default branch is the only
# real prevention, and it is UNAVAILABLE on private repos on the GitHub free plan
# (both APIs return 403). Verify protection is in force before treating the
# coder/orchestrator split as a boundary.
#
# ─── STEP 2: run this script ──────────────────────────────────────────────────
#
#   export EVENTS_TOKEN='github_pat_...'
#   export ORCHESTRATOR_TOKEN='github_pat_...'
#   export CODER_TOKEN='github_pat_...'
#   bash examples/dark-factory/setup-secrets.sh
#
# Tokens go via env vars, never argv, so they stay out of the process list. Add a
# leading space to each export if your shell honours HISTCONTROL=ignorespace —
# otherwise they land in .bash_history.
#
# Re-running is safe: an existing secret is updated in place with
# put-secret-value. external-secrets picks the change up within refreshInterval
# (1h default); the commands printed at the end force it immediately.

set -euo pipefail

REGION="${REGION:-us-west-2}"

# Both Argo AND the warm pool live on cluster `hub`: df-run's claim step creates a
# SandboxClaim with no cross-cluster mechanism, so the pool must sit beside Argo
# (agent_sandbox: true in overlays/environments/control-plane, false in dev).
#
# These are `aws_cluster_name` ANNOTATION values — the registry interpolates them
# into the SM key as <cluster>/dark-factory/github/*. They are not kubectl context
# names. Change SANDBOX only if the warm pool moves back to a spoke.
HUB="${HUB:-hub}"
SANDBOX="${SANDBOX:-hub}"

for v in EVENTS_TOKEN ORCHESTRATOR_TOKEN CODER_TOKEN; do
  if [ -z "${!v:-}" ]; then
    echo "ERROR: \$$v is not set. Export all three tokens (see the header)." >&2
    exit 1
  fi
done

for c in jq aws openssl; do
  command -v "$c" >/dev/null || { echo "ERROR: $c is required." >&2; exit 1; }
done

# Shared HMAC validating GitHub's X-Hub-Signature-256. NOT a GitHub credential —
# any strong random value. Argo Events registers the webhook with this value
# itself (trigger.argoEvents.active=true), so it is never set by hand in GitHub.
# Pass WEBHOOK_SECRET to reuse an existing one instead of rotating it.
WEBHOOK_SECRET="${WEBHOOK_SECRET:-$(openssl rand -hex 20)}"

put() {
  local name="$1" payload="$2"
  if aws secretsmanager describe-secret --region "$REGION" --secret-id "$name" >/dev/null 2>&1; then
    aws secretsmanager put-secret-value --region "$REGION" \
      --secret-id "$name" --secret-string "$payload" >/dev/null
    echo "  updated  $name"
  else
    aws secretsmanager create-secret --region "$REGION" \
      --name "$name" --secret-string "$payload" >/dev/null
    echo "  created  $name"
  fi
}

echo "Writing Dark Factory credentials to Secrets Manager ($REGION):"

# The secretKey/property names below are fixed by the consuming ExternalSecrets
# (agent-sandbox + dark-factory charts, templates/05-externalsecret-github.yaml).
# Renaming a property here makes the sync fail with "could not get secret data
# from provider" and nothing else.

# events → hub/argo-events, keys: token + webhook-secret
put "${HUB}/dark-factory/github/events" \
  "$(jq -n --arg t "$EVENTS_TOKEN" --arg w "$WEBHOOK_SECRET" \
       '{token:$t, "webhook-secret":$w}')"

# orchestrator → hub/argo, key: token
put "${HUB}/dark-factory/github/orchestrator" \
  "$(jq -n --arg t "$ORCHESTRATOR_TOKEN" '{token:$t}')"

# coder → warm-pool cluster/agent-sandbox-system, SM property `token`
# projected to the k8s key `gh-token` (the filename the coder reads at
# /etc/secrets/gh-token).
put "${SANDBOX}/dark-factory/github/coder" \
  "$(jq -n --arg t "$CODER_TOKEN" '{token:$t}')"

cat <<'EOF'

Done. Force an immediate ESO refresh rather than waiting out refreshInterval:

  kubectl --context hub -n argo                 annotate externalsecret dark-factory-github-orchestrator force-sync=$(date +%s) --overwrite
  kubectl --context hub -n argo-events          annotate externalsecret dark-factory-github-events       force-sync=$(date +%s) --overwrite
  kubectl --context hub -n agent-sandbox-system annotate externalsecret dark-factory-github-coder        force-sync=$(date +%s) --overwrite

Then confirm all three report SecretSynced / READY=True:

  kubectl --context hub get externalsecret -A | grep dark-factory-github

The warm pods are blocked on the coder secret, so once it syncs they should leave
ContainerCreating. Watch them reach Running — that is also the first real test of
whether the coder image pulls on-cluster:

  kubectl --context hub -n agent-sandbox-system get pods -w
EOF
