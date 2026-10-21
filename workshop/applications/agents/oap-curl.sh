#!/usr/bin/env bash
#
# oap-curl.sh: send a message to an agent on the Open Agentic Platform.
#
# USAGE
#   ./oap-curl.sh agent <agent-name> "<message>" [--raw]
#       Send one A2A "message/send" request and print the reply text.
#       --raw prints the full JSON-RPC response instead.
#   ./oap-curl.sh token
#       Print a Keycloak access token.
#
# Everything is discovered from the cluster in your kubectl context:
#   - the Keycloak issuer, from the AgentGateway JWT policy
#   - the AgentGateway itself, through a short-lived kubectl port-forward
#
# SETTINGS (environment variables, all optional)
#   OAP_CONTEXT    kubectl context of the cluster running the agent (default: current)
#   OAP_USERNAME   Keycloak user (default: user1)
#   OAP_PASSWORD   its password (default: $USER1_PASSWORD)
#   OAP_CLIENT     OIDC client id, a public client (default: mcp-client)
#   OAP_ISSUER     Keycloak issuer URL (default: discovered)
#   OAP_GW         gateway base URL; when set, no port-forward is started
set -euo pipefail

OAP_USERNAME="${OAP_USERNAME:-user1}"
OAP_PASSWORD="${OAP_PASSWORD:-${USER1_PASSWORD:-}}"
OAP_CLIENT="${OAP_CLIENT:-mcp-client}"
GW_NAMESPACE="agentgateway-system"
GW_SERVICE="agentgateway-proxy"
GW_PORT="8080"

die() { echo "oap-curl: $*" >&2; exit 1; }
command -v kubectl >/dev/null || die "kubectl not found"
command -v curl    >/dev/null || die "curl not found"
command -v jq      >/dev/null || die "jq not found"

KUBECTL=(kubectl)
[[ -n "${OAP_CONTEXT:-}" ]] && KUBECTL+=(--context "$OAP_CONTEXT")

# Keycloak issuer: the AgentGateway JWT policy lists the issuers it trusts.
# The Keycloak one is the issuer whose URL contains /realms/.
discover_issuer() {
  "${KUBECTL[@]}" get agentgatewaypolicy jwt-auth-policy -n "$GW_NAMESPACE" -o json 2>/dev/null \
    | jq -r '[.spec.traffic.jwtAuthentication.providers[]?.issuer | select(test("/realms/"))][0] // empty'
}

get_token() {
  local issuer="${OAP_ISSUER:-}" resp token
  [[ -n "$issuer" ]] || issuer="$(discover_issuer)"
  [[ -n "$issuer" ]] || die "could not find the Keycloak issuer on the cluster (set OAP_ISSUER)"
  [[ -n "$OAP_PASSWORD" ]] || die "no password: set OAP_PASSWORD or USER1_PASSWORD"
  resp="$(curl -s -X POST "${issuer}/protocol/openid-connect/token" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    --data-urlencode "grant_type=password" \
    --data-urlencode "client_id=${OAP_CLIENT}" \
    --data-urlencode "username=${OAP_USERNAME}" \
    --data-urlencode "password=${OAP_PASSWORD}")" || die "token request failed"
  token="$(jq -r '.access_token // empty' <<<"$resp")"
  [[ -n "$token" ]] || die "no token: $(jq -r '.error_description // .error // "unknown error"' <<<"$resp")"
  printf '%s' "$token"
}

# Open a port-forward to the gateway on a free local port and set GW_URL.
# Waits for kubectl's "Forwarding from" line instead of sleeping.
PF_PID=""
cleanup() { [[ -n "$PF_PID" ]] && kill "$PF_PID" 2>/dev/null || true; }
trap cleanup EXIT

open_gateway() {
  if [[ -n "${OAP_GW:-}" ]]; then GW_URL="$OAP_GW"; return; fi
  local fifo line port
  fifo="$(mktemp -u)"; mkfifo "$fifo"
  "${KUBECTL[@]}" -n "$GW_NAMESPACE" port-forward "svc/${GW_SERVICE}" ":${GW_PORT}" >"$fifo" 2>&1 &
  PF_PID=$!
  exec 3<"$fifo"; rm -f "$fifo"
  read -r -t 30 -u 3 line || die "port-forward to ${GW_SERVICE} did not start"
  port="$(sed -nE 's/^Forwarding from 127\.0\.0\.1:([0-9]+) .*/\1/p' <<<"$line")"
  [[ -n "$port" ]] || die "port-forward failed: $line"
  GW_URL="http://127.0.0.1:${port}"
}

cmd_agent() {
  [[ $# -ge 2 ]] || die 'usage: oap-curl.sh agent <agent-name> "<message>" [--raw]'
  local agent="$1"; shift
  local raw=0
  if [[ "${*: -1}" == "--raw" ]]; then raw=1; set -- "${@:1:$(($#-1))}"; fi
  local payload token resp
  payload="$(jq -cn --arg text "$*" --arg id "msg-$(date +%s)-$RANDOM" \
    '{jsonrpc:"2.0",id:1,method:"message/send",
      params:{message:{role:"user",messageId:$id,parts:[{kind:"text",text:$text}]}}}')"
  token="$(get_token)"
  open_gateway
  resp="$(curl -sN -X POST "${GW_URL}/${agent}" \
    -H "Authorization: Bearer ${token}" \
    -H "Content-Type: application/json" \
    -d "$payload")" || die "request to ${GW_URL}/${agent} failed"
  if [[ "$raw" -eq 1 ]]; then printf '%s\n' "$resp"; return; fi
  # The agent streams its reply as many small text parts; join them.
  jq -r '
    if .error then "error: " + (.error.message // (.error | tostring))
    elif ((.result.artifacts // []) | length) > 0 then
      [.result.artifacts[].parts[]?.text // empty] | join("")
    else "(no reply text in response)" end' <<<"$resp" 2>/dev/null \
    || printf '%s\n' "$resp"
}

case "${1:-}" in
  agent) shift; cmd_agent "$@" ;;
  token) get_token; echo ;;
  *) sed -n '3,22p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
