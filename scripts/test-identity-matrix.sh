#!/usr/bin/env bash
# Agent identity at the gateway: live scenario matrix. Prints PASS/FAIL per row;
# exit code = number of failed rows.
#
# Runs every request from a short-lived curl pod inside the cluster, through the
# gateway Service, so it needs no public gateway URL.
#
# Required:
#   KEYCLOAK_HOST    host serving /keycloak/realms/<realm> (e.g. the CloudFront domain)
# Optional (defaults in brackets):
#   SPOKE_KUBECONFIG cluster running the gateway and the agents [$KUBECONFIG]
#   HUB_KUBECONFIG   cluster holding the keycloak/keycloak-config secret [$SPOKE_KUBECONFIG]
#   REALM [platform]  CLIENT [mcp-client]  USERNAME [user1]
#   AGENT [oap-assistant-a]  AGENT_NS [default]   an agent listed in the server's allowedAgents
#   MCP [mcp-time]                                an MCP server with an allowedAgents list
#   OTHER_SA [default]                            a ServiceAccount NOT in that list
#   AUDIENCE [agentgateway]
set -uo pipefail
: "${KEYCLOAK_HOST:?set KEYCLOAK_HOST}"
SPOKE_KUBECONFIG="${SPOKE_KUBECONFIG:-${KUBECONFIG:?set SPOKE_KUBECONFIG or KUBECONFIG}}"
HUB_KUBECONFIG="${HUB_KUBECONFIG:-$SPOKE_KUBECONFIG}"
REALM="${REALM:-platform}" CLIENT="${CLIENT:-mcp-client}" USERNAME="${USERNAME:-user1}"
AGENT="${AGENT:-oap-assistant-a}" AGENT_NS="${AGENT_NS:-default}" MCP="${MCP:-mcp-time}"
OTHER_SA="${OTHER_SA:-default}" AUDIENCE="${AUDIENCE:-agentgateway}"
GW=http://agentgateway-proxy.agentgateway-system.svc.cluster.local:8080
POD="identity-matrix-probe-$(date +%s)"  # unique per run: a previous run's probe may still be terminating
FAILS=0

ks() { KUBECONFIG="$SPOKE_KUBECONFIG" kubectl --request-timeout=30s "$@"; }
kh() { KUBECONFIG="$HUB_KUBECONFIG" kubectl --request-timeout=30s "$@"; }
row() {
  if [ "$3" = "$4" ]; then printf 'PASS  %-4s %-70s %s\n' "$1" "$2" "$4"
  else printf 'FAIL  %-4s %-70s expected=%s actual=%s\n' "$1" "$2" "$3" "$4"; FAILS=$((FAILS + 1)); fi
}
# code <token|-> <method> <path> [body] -> HTTP status from inside the cluster
code() {
  local t=$1 m=$2 p=$3 b=${4:-}
  local args=(-s -o /dev/null -m 120 -w '%{http_code}' -X "$m" "$GW$p")
  [ "$t" != - ] && args+=(-H "Authorization: Bearer $t")
  [ -n "$b" ] && args+=(-H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d "$b")
  ks exec "$POD" -n "$AGENT_NS" -- curl "${args[@]}"
}
body() {
  ks exec "$POD" -n "$AGENT_NS" -- curl -s -m 180 -X POST "$GW$2" -H "Authorization: Bearer $1" \
    -H 'Content-Type: application/json' -d "$3"
}

PW=$(kh get secret keycloak-config -n keycloak -o jsonpath='{.data.USER_PASSWORD}' | base64 -d)
USER_TOKEN=$(curl -s -X POST "https://$KEYCLOAK_HOST/keycloak/realms/$REALM/protocol/openid-connect/token" \
  -d "client_id=$CLIENT" -d "username=$USERNAME" -d "password=$PW" -d grant_type=password |
  python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')
AGENT_TOKEN=$(ks create token "$AGENT" -n "$AGENT_NS" --audience "$AUDIENCE" --duration 10m)
OTHER_TOKEN=$(ks create token "$OTHER_SA" -n "$AGENT_NS" --audience "$AUDIENCE" --duration 10m)
WRONG_AUD_TOKEN=$(ks create token "$AGENT" -n "$AGENT_NS" --audience not-the-gateway --duration 10m)
ISSUER=$(python3 -c 'import sys,json,base64;p=sys.argv[1].split(".")[1];p+="="*(-len(p)%4);print(json.loads(base64.urlsafe_b64decode(p))["iss"])' "$AGENT_TOKEN")

ks run "$POD" -n "$AGENT_NS" --restart=Never --image=curlimages/curl:8.10.1 --command -- sleep 900 >/dev/null
ks wait --for=condition=Ready "pod/$POD" -n "$AGENT_NS" --timeout=120s >/dev/null
trap 'ks delete pod "$POD" -n "$AGENT_NS" --ignore-not-found --wait=false >/dev/null 2>&1' EXIT

TOOLS='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"identity-matrix","version":"1"}}}'
START=$(date -u +%Y-%m-%dT%H:%M:%SZ)
echo "=== gateway rule (live object) ==="
RULE=$(ks get agentgatewaypolicy jwt-auth-policy -n agentgateway-system -o jsonpath='{.spec.traffic.authorization.policy.matchExpressions[0]}')
row I1 "workload branch pins jwt.iss to the cluster issuer" yes "$(echo "$RULE" | grep -qF "jwt.iss == \"$ISSUER\"" && echo yes || echo no)"
row I2 "human branch excludes ServiceAccount-shaped subjects" yes "$(echo "$RULE" | grep -qF '&& !(has(jwt.sub) && jwt.sub.startsWith("system:serviceaccount:"))' && echo yes || echo no)"

echo "=== who may reach what ==="
row U1 "user -> agent route" 200 "$(code "$USER_TOKEN" POST "/$AGENT/chat" '{"message":"Say hi."}')"
row U2 "no token -> agent route" 401 "$(code - POST "/$AGENT/chat" '{"message":"Say hi."}')"
row U3 "user -> MCP server with allowedAgents (not an agent)" 403 "$(code "$USER_TOKEN" POST "/mcp/$MCP" "$TOOLS")"
row W1 "listed agent's ServiceAccount token -> that MCP server" 200 "$(code "$AGENT_TOKEN" POST "/mcp/$MCP" "$TOOLS")"
row W2 "unlisted ServiceAccount token -> that MCP server" 403 "$(code "$OTHER_TOKEN" POST "/mcp/$MCP" "$TOOLS")"
row W3 "unlisted ServiceAccount token -> agent route (Gateway rule only)" 200 "$(code "$OTHER_TOKEN" POST "/$AGENT/chat" '{"message":"Say hi."}')"
row W4 "ServiceAccount token with the wrong audience -> agent route" 401 "$(code "$WRONG_AUD_TOKEN" POST "/$AGENT/chat" '{"message":"Say hi."}')"

echo "=== end to end ==="
# The model sometimes answers in text instead of calling the tool, so ask up to twice and
# judge by the gateway log: the agent reached the MCP server with ITS OWN identity.
SUB="system:serviceaccount:$AGENT_NS:$AGENT"
gw_hit() { ks logs -n agentgateway-system -l app.kubernetes.io/name=agentgateway-proxy --since-time="$START" --tail=-1 2>/dev/null |
  grep "route=$AGENT_NS/$MCP " | grep "jwt.sub=$SUB " | grep -v 'http.method=GET' | grep -c 'http.status=200'; }
BASE=$(gw_hit)
for _ in 1 2; do
  R=$(body "$USER_TOKEN" "/$AGENT/chat" '{"message":"What time is it in UTC right now? Call your time tool. Reply with the time only."}')
  [ "$(gw_hit)" -gt "$BASE" ] && break
done
row E1 "user asks the agent; agent calls the MCP tool as $SUB" yes "$([ "$(gw_hit)" -gt "$BASE" ] && echo yes || echo no)"
echo "      reply: $(echo "$R" | head -c 200)"
LOG=$(for p in $(ks get pods -n "$AGENT_NS" -l "app.kubernetes.io/name=$AGENT" -o name); do
  ks logs -n "$AGENT_NS" "$p" -c "$AGENT" --tail=-1; done 2>/dev/null)
PODS=$(ks get pods -n "$AGENT_NS" -l "app.kubernetes.io/name=$AGENT" -o name | grep -c .)
row E2 "every agent pod logs its MCP identity mode at startup" "$PODS" "$(echo "$LOG" | grep -c 'MCP identity: agent ServiceAccount token')"

echo "=== MCP server definition ==="
# Issue #61: an MCP server Application that omits authPolicy and uses the
# aws-service-identity trait was rejected at admission. Server-side dry run goes through
# the KubeVela webhook, the same path ArgoCD and kubectl apply take.
EX="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/platform/oam/examples/example-browser-mcp.yaml"
row D1 "shipped browser MCP example passes KubeVela admission" yes \
  "$(ks apply --dry-run=server -f "$EX" >/dev/null 2>&1 && echo yes || echo no)"

echo "NOT RUN  K1   Keycloak-issued token with a ServiceAccount-shaped sub -> MCP server: 403 expected."
echo "             Needs Keycloak to mint such a sub (e.g. a protocol mapper); I1/I2 check the rule instead."
echo "FAILED ROWS: $FAILS"
exit "$FAILS"
