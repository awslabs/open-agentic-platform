#!/usr/bin/env bash
# s3-vectors-mcp: live scenario matrix. Prints PASS/FAIL per row; exit code = failed rows.
#
#   PHASE=setup     create the test vector bucket and indexes, seed them (standing in
#                   for the ingestion pipeline, which is outside OAP), and apply the
#                   test Applications
#   PHASE=run       run the rows (default)
#   PHASE=teardown  delete the test Applications, then the vector bucket
#
# Requires AWS credentials for the cluster's account, and the s3-vectors-mcp
# ComponentDefinition synced to the cluster.
# Optional (defaults in brackets):
#   SPOKE_KUBECONFIG [$KUBECONFIG]   REGION [us-west-2]   NS [s3v-matrix]
#   BUCKET [oap-s3v-matrix]          AUDIENCE [agentgateway]
#   PROBE_IMAGE [public.ecr.aws/z0a4o2j5/strands-agent:v1.3.3-main] (ships the mcp client)
set -uo pipefail
PHASE="${PHASE:-run}"
SPOKE_KUBECONFIG="${SPOKE_KUBECONFIG:-${KUBECONFIG:?set SPOKE_KUBECONFIG or KUBECONFIG}}"
REGION="${REGION:-us-west-2}" NS="${NS:-s3v-matrix}" BUCKET="${BUCKET:-oap-s3v-matrix}"
AUDIENCE="${AUDIENCE:-agentgateway}"
PROBE_IMAGE="${PROBE_IMAGE:-public.ecr.aws/z0a4o2j5/strands-agent:v1.3.3-main}"
MODEL=amazon.titan-embed-text-v2:0
GW=http://agentgateway-proxy.agentgateway-system.svc.cluster.local:8080
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FAILS=0

ks() { KUBECONFIG="$SPOKE_KUBECONFIG" kubectl --request-timeout=30s "$@"; }
s3v() { aws s3vectors --region "$REGION" "$@"; }
row() {
  if [ "$3" = "$4" ]; then printf 'PASS  %-4s %-72s %s\n' "$1" "$2" "$4"
  else printf 'FAIL  %-4s %-72s expected=%s actual=%s\n' "$1" "$2" "$3" "$4"; FAILS=$((FAILS + 1)); fi
}

# ---------------------------------------------------------------------------------
# Test Applications. Positive: docs-search (allowedAgents: docs-assistant) and the
# agent that uses it. Negative: an index the model cannot match, and a server whose
# trait attaches no policy.
# ---------------------------------------------------------------------------------
apps() {
  cat <<EOF
apiVersion: core.oam.dev/v1beta1
kind: Application
metadata: {name: s3v-matrix, namespace: $NS}
spec:
  components:
    - name: docs-search
      type: s3-vectors-mcp
      properties:
        vectorBucket: $BUCKET
        index: docs
        description: ACME product policies, the X200 user guide and release notes
        allowedAgents: [docs-assistant]
        maxTopK: 5
      traits:
        - type: aws-service-identity
          properties: {accessFor: [docs-search]}
    - name: docs-assistant
      type: agent
      dependsOn: [docs-search]
      properties:
        description: Answers questions from the ACME documentation
        mcpServers: [{name: docs-search}]
        systemMessage: >-
          Answer only from the ACME documentation. Always call the docs-search
          search tool first, then answer briefly and cite the source.
      traits:
        - type: gateway-identity
---
apiVersion: core.oam.dev/v1beta1
kind: Application
metadata: {name: s3v-matrix-neg, namespace: $NS}
spec:
  components:
    - name: mismatch-search
      type: s3-vectors-mcp
      properties: {vectorBucket: $BUCKET, index: docs-384}
      traits:
        - type: aws-service-identity
          properties: {accessFor: [mismatch-search]}
    - name: noaccess-search
      type: s3-vectors-mcp
      properties: {vectorBucket: $BUCKET, index: docs}
      traits:
        - type: aws-service-identity
EOF
}

seed() {  # seed <index> <key> <source> <page> <text>
  local emb
  aws bedrock-runtime invoke-model --region "$REGION" --model-id "$MODEL" \
    --cli-binary-format raw-in-base64-out \
    --body "$(python3 -c 'import json,sys;print(json.dumps({"inputText":sys.argv[1],"dimensions":1024,"normalize":True}))' "$5")" \
    /tmp/s3v-emb.json >/dev/null || return 1
  emb=$(python3 -c 'import json;print(json.dumps(json.load(open("/tmp/s3v-emb.json"))["embedding"]))')
  python3 - "$2" "$3" "$4" "$5" "$emb" > /tmp/s3v-put.json <<'PY'
import json, sys
key, source, page, text, emb = sys.argv[1:]
print(json.dumps([{"key": key, "data": {"float32": json.loads(emb)},
                   "metadata": {"text": text, "source": source, "page": int(page)}}]))
PY
  s3v put-vectors --vector-bucket-name "$BUCKET" --index-name "$1" --vectors file:///tmp/s3v-put.json
}

if [ "$PHASE" = setup ]; then
  s3v get-vector-bucket --vector-bucket-name "$BUCKET" >/dev/null 2>&1 || s3v create-vector-bucket --vector-bucket-name "$BUCKET"
  for spec in "docs 1024" "docs-384 384" "other 1024"; do
    set -- $spec
    s3v get-index --vector-bucket-name "$BUCKET" --index-name "$1" >/dev/null 2>&1 ||
      s3v create-index --vector-bucket-name "$BUCKET" --index-name "$1" --data-type float32 \
        --dimension "$2" --distance-metric cosine \
        --metadata-configuration '{"nonFilterableMetadataKeys":["text"]}'
  done
  # A just-created index can answer NotFound for a few seconds. Setup is idempotent:
  # if seeding fails with NotFound, run it again.
  seed docs refund policies.pdf 2 "Refunds are issued within 5 business days to the original payment method."
  seed docs shipping policies.pdf 4 "Standard shipping takes 3 to 7 days; express shipping takes 1 to 2 days."
  seed docs warranty x200-guide.pdf 12 "The X200 has a two-year limited warranty covering manufacturing defects."
  seed docs reset x200-guide.pdf 30 "To factory reset the X200, hold the power button for 15 seconds."
  seed docs release-4-2 release-notes.md 1 "Release 4.2 adds offline mode and fixes a battery drain bug."
  seed docs cafeteria handbook.md 7 "The office cafeteria opens at 8am on weekdays."
  rm -f /tmp/s3v-put.json /tmp/s3v-emb.json
  ks get ns "$NS" >/dev/null 2>&1 || ks create ns "$NS"
  apps | ks apply -f -
  echo "setup done; the Applications need a few minutes (Pod Identity + IAM propagation)"
  exit 0
fi

if [ "$PHASE" = teardown ]; then
  apps | ks delete -f - --ignore-not-found --wait=true --timeout=280s
  for i in docs docs-384 other; do s3v delete-index --vector-bucket-name "$BUCKET" --index-name "$i" 2>/dev/null; done
  s3v delete-vector-bucket --vector-bucket-name "$BUCKET"
  exit 0
fi

# ---------------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------------
POD="s3v-matrix-probe-$(date +%s)"
ks run "$POD" -n "$NS" --restart=Never --image="$PROBE_IMAGE" --command -- sleep 900 >/dev/null
ks wait --for=condition=Ready "pod/$POD" -n "$NS" --timeout=120s >/dev/null
trap 'ks delete pod "$POD" -n "$NS" --ignore-not-found --wait=false >/dev/null 2>&1' EXIT
probe() {  # probe <token> <server> list | call <tool> <json>
  local t=$1 s=$2; shift 2
  ks exec -i "$POD" -n "$NS" -- python - "$GW/mcp/$s" "$t" "$@" < "$HERE/s3-vectors-probe.py"
}
jq_() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)" 2>/dev/null || echo parse-error; }
# http <method> <url> [token] [body] -> "<status> <body>" from inside the cluster
http() { ks exec "$POD" -n "$NS" -- python -c '
import sys, urllib.request, urllib.error
m, url, tok, body = (sys.argv[1:] + ["", ""])[:4]
h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
if tok: h["Authorization"] = "Bearer " + tok
req = urllib.request.Request(url, data=body.encode() if body else None, headers=h, method=m)
try:
    r = urllib.request.urlopen(req, timeout=180); print(r.status, r.read().decode()[:4000])
except urllib.error.HTTPError as e:
    print(e.code, e.read().decode()[:500])
except Exception as e:
    print("blocked", type(e).__name__)
' "$@"; }
INIT='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"m","version":"1"}}}'
status() { http POST "$GW/mcp/$1" "${2:-}" "$INIT" | cut -d' ' -f1; }
readyz() { http GET "http://$(ks get pod -n "$NS" -l "app.kubernetes.io/name=$1" -o jsonpath='{.items[0].status.podIP}'):8000/readyz"; }

AGENT_TOKEN=$(ks create token docs-assistant -n "$NS" --audience "$AUDIENCE" --duration 10m)
OTHER_TOKEN=$(ks create token default -n "$NS" --audience "$AUDIENCE" --duration 10m)

echo "=== fixture and rollout ==="
row F1 "fixture index docs is 1024-dim cosine" "1024 cosine" \
  "$(s3v get-index --vector-bucket-name "$BUCKET" --index-name docs --query 'index.[dimension,distanceMetric]' --output text | tr '\t' ' ')"
row R1 "docs-search Rollout is Healthy" Healthy "$(ks get rollout docs-search -n "$NS" -o jsonpath='{.status.phase}')"
POLICY=$(ks get policies.iam.aws.upbound.io "s3v-matrix-docs-search-iam-policy" -o jsonpath='{.spec.forProvider.policy}')
ACCT=$(aws sts get-caller-identity --query Account --output text)
row P1 "IAM policy is scoped to exactly this index and model" yes "$(echo "$POLICY" | python3 -c '
import json,sys; d=json.load(sys.stdin); r=sorted(s["Resource"] for s in d["Statement"])
print("yes" if r==sorted(["arn:aws:s3vectors:'"$REGION:$ACCT"':bucket/'"$BUCKET"'/index/docs","arn:aws:bedrock:'"$REGION"'::foundation-model/'"$MODEL"'"]) else r)')"

echo "=== through the gateway, as the allowed agent ==="
row T1 "tools/list exposes only read tools" "describe_index,search" \
  "$(probe "$AGENT_TOKEN" docs-search list | jq_ '",".join(d.get("tools",[d.get("error")]))')"
row T2 "describe_index reports the index" "1024 cosine" \
  "$(probe "$AGENT_TOKEN" docs-search call describe_index '{}' | jq_ 'd["result"]["dimension"],d["result"]["distanceMetric"]' | tr -d "(),'" )"
S=$(probe "$AGENT_TOKEN" docs-search call search '{"query":"How long does a refund take?","top_k":3}')
row T3 "search ranks the refund chunk first" refund "$(echo "$S" | jq_ 'd["result"]["results"][0]["key"]')"
row T4 "result carries chunk text and filterable metadata" "policies.pdf 2 True" \
  "$(echo "$S" | jq_ 'd["result"]["results"][0]["metadata"]["source"], d["result"]["results"][0]["metadata"]["page"], "5 business days" in d["result"]["results"][0]["text"]' | tr -d "(),'")"
row T5 "metadata filter restricts results to one source" "x200-guide.pdf" \
  "$(probe "$AGENT_TOKEN" docs-search call search '{"query":"refund","top_k":5,"filter":{"source":{"$eq":"x200-guide.pdf"}}}' |
     jq_ '",".join(sorted({r["metadata"]["source"] for r in d["result"]["results"]}))')"
row T6 "top_k above maxTopK is a tool error" True \
  "$(probe "$AGENT_TOKEN" docs-search call search '{"query":"refund","top_k":50}' | jq_ 'd.get("isError")')"

echo "=== who may call it ==="
row A1 "no token -> 401" 401 "$(status docs-search)"
row A2 "ServiceAccount not in allowedAgents -> 403" 403 "$(status docs-search "$OTHER_TOKEN")"
row A3 "allowed agent -> 200" 200 "$(status docs-search "$AGENT_TOKEN")"

echo "=== IAM boundary (from inside the server pod, with its own role) ==="
SP=$(ks get pod -n "$NS" -l app.kubernetes.io/name=docs-search -o jsonpath='{.items[0].metadata.name}')
iam() { ks exec "$SP" -n "$NS" -c docs-search -- python -c "
import boto3,botocore
c=boto3.client('s3vectors',region_name='$REGION')
try:
    $1
    print('allowed')
except botocore.exceptions.ClientError as e:
    print(e.response['Error']['Code'])"; }
row B1 "QueryVectors on another index in the same bucket is denied" AccessDeniedException \
  "$(iam "c.query_vectors(vectorBucketName='$BUCKET',indexName='other',queryVector={'float32':[0.0]*1024},topK=1)")"
row B2 "PutVectors on its own index is denied (read-only)" AccessDeniedException \
  "$(iam "c.put_vectors(vectorBucketName='$BUCKET',indexName='docs',vectors=[{'key':'x','data':{'float32':[0.0]*1024}}])")"

echo "=== startup failures are visible, not silent ==="
row N1 "index the model cannot match -> not Ready" False \
  "$(ks get pod -n "$NS" -l app.kubernetes.io/name=mismatch-search -o jsonpath='{.items[0].status.containerStatuses[0].ready}')"
row N2 "... and /readyz names the dimension mismatch" yes "$(readyz mismatch-search | grep -q 'index has 384' && echo yes || echo no)"
row N3 "trait without accessFor -> not Ready" False \
  "$(ks get pod -n "$NS" -l app.kubernetes.io/name=noaccess-search -o jsonpath='{.items[0].status.containerStatuses[0].ready}')"
row N4 "... and /readyz names the missing accessFor entry" yes \
  "$(readyz noaccess-search | grep -q 'accessFor: \[noaccess-search\]' && echo yes || echo no)"

echo "=== end to end: agent answers from the index ==="
START=$(date -u +%Y-%m-%dT%H:%M:%SZ)
SUB="system:serviceaccount:$NS:docs-assistant"
hits() { ks logs -n agentgateway-system -l app.kubernetes.io/name=agentgateway-proxy --since-time="$START" --tail=-1 2>/dev/null |
  grep "route=$NS/docs-search " | grep "jwt.sub=$SUB " | grep -c 'http.status=200'; }
for _ in 1 2; do
  R=$(http POST "$GW/docs-assistant/chat" "$OTHER_TOKEN" '{"message":"How long do refunds take? Use the documentation search tool."}')
  [ "$(hits)" -gt 0 ] && break
done
row E1 "agent called docs-search as its own ServiceAccount" yes "$([ "$(hits)" -gt 0 ] && echo yes || echo no)"
row E2 "agent answer contains the indexed fact" yes "$(echo "$R" | grep -qi '5 business days' && echo yes || echo no)"
echo "      reply: $(echo "$R" | head -c 240)"

echo "=== network policy ==="
NPC=$(ks get cm amazon-vpc-cni -n kube-system -o jsonpath='{.data.enable-network-policy-controller}' 2>/dev/null)
DIRECT=$(http GET "http://docs-search-stable.$NS.svc.cluster.local/readyz" | cut -d' ' -f1)
if [ "$NPC" = true ]; then
  row X1 "direct call to the server, bypassing the gateway, is blocked" blocked "$DIRECT"
else
  echo "NOT ENFORCED  X1  direct call bypassing the gateway returned $DIRECT: this cluster has no network"
  echo "                  policy controller (kube-system/amazon-vpc-cni enable-network-policy-controller)."
fi

echo "FAILED ROWS: $FAILS"
exit "$FAILS"
