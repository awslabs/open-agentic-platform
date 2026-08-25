#!/usr/bin/env bash
# deploy-test.sh — content-aware verification of a PR's deployable artifacts.
# Runs in the TRUSTED hub deploy-test step (the only step with K8s access — never
# the coder). Shipped as a ConfigMap file (not inline in the WorkflowTemplate) so
# there are no YAML-block-scalar quoting hazards. Kind-driven so it generalizes to
# future profiles (terraform | k8s | ... ).
#
# Env: GH_TOKEN, REPO (owner/name), BRANCH, ISSUE_NUMBER, KIND, MANIFEST_PATH,
#      TF_PATH, READY_TIMEOUT, BLOCKING, WF_NAME
set -eu

WORK=/tmp/dt
rm -rf "$WORK"; mkdir -p "$WORK"
git clone --quiet --depth 1 --branch "$BRANCH" \
  "https://x-access-token:${GH_TOKEN}@github.com/${REPO}.git" "$WORK/repo"
cd "$WORK/repo"

STATE=success
DESC="deploy-test passed"
RC=0
FENCE='```'
REPORT=/tmp/report.md
: > "$REPORT"

# Pod Security + quota for the ephemeral test namespace (review: HIGH).
PSS_MODE="${PSS_MODE:-restricted}"
QUOTA_PODS="${QUOTA_PODS:-20}"
QUOTA_CPU="${QUOTA_CPU:-4}"
QUOTA_MEMORY="${QUOTA_MEMORY:-8Gi}"

# Create the ephemeral namespace WITH Pod Security enforcement and a quota in ONE
# apply, before any coder-written manifest can land in it.
#
# Why atomic: `kubectl create namespace` then `kubectl label` leaves a window where
# the namespace exists UNLABELLED, and PodSecurity is evaluated at pod ADMISSION —
# anything applied in that window is never checked. These manifests are written by
# the model, so a privileged pod with a hostPath mount would mean root on the node.
#
# TRADEOFF: enforce=restricted also rejects ordinary images that run as root or omit
# seccomp/capabilities — a plain nginx manifest WILL be refused and reported as a
# failure. That is the fail-secure default; set deployTest.podSecurityStandard to
# `baseline` to keep blocking privileged/hostPath/host-namespaces while allowing
# root containers.
create_test_ns() {
  kubectl apply -f - >/dev/null 2>&1 <<EOF
apiVersion: v1
kind: Namespace
metadata:
  name: ${NS}
  labels:
    dark-factory.io/ephemeral: "true"
    dark-factory.io/issue-number: "${ISSUE_NUMBER}"
    pod-security.kubernetes.io/enforce: "${PSS_MODE}"
    pod-security.kubernetes.io/enforce-version: latest
    pod-security.kubernetes.io/audit: "${PSS_MODE}"
    pod-security.kubernetes.io/warn: "${PSS_MODE}"
---
apiVersion: v1
kind: ResourceQuota
metadata:
  name: df-test-quota
  namespace: ${NS}
spec:
  hard:
    pods: "${QUOTA_PODS}"
    requests.cpu: "${QUOTA_CPU}"
    requests.memory: "${QUOTA_MEMORY}"
    limits.cpu: "${QUOTA_CPU}"
    limits.memory: "${QUOTA_MEMORY}"
    persistentvolumeclaims: "0"
    services.loadbalancers: "0"
    services.nodeports: "0"
EOF
}

case "$KIND" in
  terraform)
    cd "$TF_PATH"
    echo "[deploy-test] terraform validate in $(pwd) ($(terraform version | head -1))"
    if terraform init -backend=false -input=false -no-color >/tmp/tf.log 2>&1 \
       && terraform validate -no-color >>/tmp/tf.log 2>&1; then
      FMT_N="$(terraform fmt -check -recursive -no-color 2>/dev/null | wc -l | tr -d ' ' || echo 0)"
      echo "[deploy-test] terraform validate OK (fmt: ${FMT_N} unformatted)"
      DESC="terraform validate passed"
      echo "\`terraform init -backend=false\` + \`terraform validate\` passed. (fmt: ${FMT_N} file(s) not formatted)" >> "$REPORT"
    else
      echo "[deploy-test] terraform init/validate FAILED"; tail -30 /tmp/tf.log || true
      STATE=failure; DESC="terraform validate failed"; RC=1
      { echo "\`terraform init/validate\` failed:"; echo "$FENCE"; tail -20 /tmp/tf.log; echo "$FENCE"; } >> "$REPORT"
    fi
    cd "$WORK/repo"
    ;;

  k8s)
    NS="df-test-${ISSUE_NUMBER}-${WF_NAME}"
    NS="$(echo "$NS" | tr '[:upper:]' '[:lower:]' | cut -c1-63)"
    cleanup() { echo "[deploy-test] tearing down namespace $NS"; kubectl delete namespace "$NS" --ignore-not-found --wait=false >/dev/null 2>&1 || true; }
    trap cleanup EXIT
    if [ ! -e "$MANIFEST_PATH" ]; then
      echo "[deploy-test] no manifests at '$MANIFEST_PATH' (advisory)"
      DESC="no manifests at $MANIFEST_PATH"
      echo "No manifests found at \`$MANIFEST_PATH\`." >> "$REPORT"
    elif create_test_ns \
         && echo "[deploy-test] namespace $NS ready (PodSecurity enforce=${PSS_MODE}, quota ${QUOTA_PODS} pods / ${QUOTA_CPU} cpu / ${QUOTA_MEMORY})" \
         && kubectl apply -n "$NS" -f "$MANIFEST_PATH" >/tmp/apply.log 2>&1; then
      cat /tmp/apply.log
      echo "[deploy-test] waiting up to ${READY_TIMEOUT}s for workloads Available..."
      if kubectl wait -n "$NS" --for=condition=Available --timeout="${READY_TIMEOUT}s" deploy --all >/dev/null 2>&1; then
        echo "[deploy-test] all Deployments Available"
        echo "Deployed to an ephemeral namespace; all Deployments became Available." >> "$REPORT"
      else
        BAD="$(kubectl get pods -n "$NS" --no-headers 2>/dev/null | grep -cE 'CrashLoopBackOff|Error|ImagePullBackOff' || true)"
        if [ "${BAD:-0}" != "0" ]; then
          STATE=failure; DESC="deploy-test: ${BAD} pod(s) not healthy"; RC=1
          echo "${BAD} pod(s) not healthy after apply." >> "$REPORT"
        else
          echo "Applied; no Deployments to wait on." >> "$REPORT"
        fi
      fi
    else
      cat /tmp/apply.log 2>/dev/null || true
      RC=1; STATE=failure
      if grep -qi "violates PodSecurity" /tmp/apply.log 2>/dev/null; then
        # A real finding, not harness noise: the manifest wants privileges an
        # ephemeral test namespace refuses (privileged, hostPath, host namespaces,
        # or running as root under `restricted`).
        DESC="deploy-test: manifest rejected by PodSecurity (enforce=${PSS_MODE})"
        { echo "Manifest **rejected by PodSecurity** (\`enforce=${PSS_MODE}\`) — it requests privileges an ephemeral test namespace does not grant:"; echo "$FENCE"; tail -20 /tmp/apply.log 2>/dev/null; echo "$FENCE"; } >> "$REPORT"
      else
        DESC="kubectl apply failed"
        { echo "\`kubectl apply\` failed:"; echo "$FENCE"; tail -20 /tmp/apply.log 2>/dev/null; echo "$FENCE"; } >> "$REPORT"
      fi
    fi
    ;;

  *)
    echo "[deploy-test] unknown kind '$KIND' — nothing to do"
    DESC="no deploy test for kind=$KIND"
    echo "No deploy test defined for kind \`$KIND\`." >> "$REPORT"
    ;;
esac

SHA="$(git rev-parse HEAD)"

# 1) commit status
curl -fsS -X POST -H "Authorization: Bearer ${GH_TOKEN}" -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/${REPO}/statuses/${SHA}" \
  -d "{\"state\":\"${STATE}\",\"context\":\"dark-factory/deploy-test\",\"description\":\"${DESC}\"}" >/dev/null 2>&1 \
  && echo "[deploy-test] posted dark-factory/deploy-test=${STATE}" \
  || echo "[deploy-test] WARN: status post failed"

# 2) findings PR comment (marker upsert via comment.js)
ICON="✅"; [ "$STATE" = success ] || ICON="❌"
PR="$(curl -fsS -H "Authorization: Bearer ${GH_TOKEN}" \
      "https://api.github.com/repos/${REPO}/pulls?head=${REPO%%/*}:${BRANCH}&state=open" 2>/dev/null \
      | node -e 'let s="";process.stdin.on("data",d=>s+=d).on("end",()=>{try{console.log(JSON.parse(s)[0].number||"")}catch(e){}})' 2>/dev/null || echo "")"
if [ -n "$PR" ]; then
  { echo "### ${ICON} Deploy test (${KIND})"; echo "**${DESC}**"; echo; cat "$REPORT"; } \
    | GH_TOKEN="$GH_TOKEN" REPO="$REPO" PR="$PR" node /scripts/comment.js "dark-factory:deploy-test"
fi

if [ "$BLOCKING" = "true" ]; then exit "$RC"; else exit 0; fi
