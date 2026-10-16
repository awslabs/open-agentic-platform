#!/usr/bin/env bash
# AgentCore memory: live scenario matrix through the gateway. PASS/FAIL per row;
# exit code = number of failed rows.
#
# Deploy platform/oam/examples/example-agent-agentcore-memory.yaml first, then:
#   PHASE=write  provisioning rows, fail-closed rows, user1 states facts (session A)
#   PHASE=read   recall and isolation rows. Run after AgentCore has extracted the
#                long-term records (a few minutes); L1 says so if it has not.
#
# Required: KEYCLOAK_HOST (serves /keycloak/realms/platform), PHASE
# Optional: SPOKE_KUBECONFIG [$KUBECONFIG], HUB_KUBECONFIG [$SPOKE_KUBECONFIG] (holds
#           keycloak/keycloak-config), STATE [${TMPDIR:-/tmp}/memory-matrix.env]
# Uses Keycloak users user1 and user2, and the aws CLI with read access to AgentCore.
set -uo pipefail
: "${KEYCLOAK_HOST:?set KEYCLOAK_HOST}" "${PHASE:?set PHASE=write or PHASE=read}"
SPOKE_KUBECONFIG="${SPOKE_KUBECONFIG:-${KUBECONFIG:?set SPOKE_KUBECONFIG or KUBECONFIG}}"
HUB_KUBECONFIG="${HUB_KUBECONFIG:-$SPOKE_KUBECONFIG}"
ks() { KUBECONFIG="$SPOKE_KUBECONFIG" kubectl --request-timeout=30s "$@"; }
kh() { KUBECONFIG="$HUB_KUBECONFIG" kubectl --request-timeout=30s "$@"; }
NS=agents; AGENT=my-agent; MEM=agents-agentcore-short-memory  # <namespace>-<component>
GW=http://agentgateway-proxy.agentgateway-system.svc.cluster.local:8080
STATE="${STATE:-${TMPDIR:-/tmp}/memory-matrix.env}"; FAILS=0
row() { if [ "$3" = "$4" ]; then printf 'PASS  %-4s %-66s %s\n' "$1" "$2" "$3"; else printf 'FAIL  %-4s %-66s expected=%s actual=%s\n' "$1" "$2" "$3" "$4"; FAILS=$((FAILS+1)); fi; }
yn() { [ "$1" ] && echo yes || echo no; }

UPW=$(kh get secret keycloak-config -n keycloak -o jsonpath='{.data.USER_PASSWORD}' | base64 -d)
tok() { curl -s -X POST "https://$KEYCLOAK_HOST/keycloak/realms/platform/protocol/openid-connect/token" -d client_id=mcp-client -d "username=$1" -d "password=$UPW" -d grant_type=password | python3 -c 'import json,sys;print(json.load(sys.stdin)["access_token"])'; }
sub() { python3 -c 'import sys,json,base64;p=sys.argv[1].split(".")[1];p+="="*(-len(p)%4);print(json.loads(base64.urlsafe_b64decode(p))["sub"])' "$1"; }
U1=$(tok user1); U2=$(tok user2); S1=$(sub "$U1"); S2=$(sub "$U2")
MEMID=$(ks get memories.bedrockagentcore.aws.upbound.io $MEM -o jsonpath='{.metadata.annotations.crossplane\.io/external-name}')
CLUSTER_REGION=$(ks get nodes -o jsonpath='{.items[0].metadata.labels.topology\.kubernetes\.io/region}')
REGION=$(ks get memories.bedrockagentcore.aws.upbound.io $MEM -o jsonpath='{.spec.forProvider.region}')

PROBE="ltm-probe-$(date +%s)"
ks run $PROBE -n default --restart=Never --image=curlimages/curl:8.10.1 --command -- sleep 900 >/dev/null
ks wait --for=condition=Ready pod/$PROBE -n default --timeout=90s >/dev/null
trap 'ks delete pod $PROBE -n default --ignore-not-found --wait=false >/dev/null 2>&1' EXIT
echo "agent image: $(ks get pods -n $NS -l app.kubernetes.io/name=$AGENT -o jsonpath='{.items[0].spec.containers[0].image}')"
# chat <token> <contextId> <message> [extra curl args] -> prints "<http code> <response text>"
chat() { local t=$1 c=$2 m=$3; shift 3
  ks exec $PROBE -n default -- curl -s -m 180 -w '\n%{http_code}' -X POST "$GW/$AGENT/chat" \
    -H 'Content-Type: application/json' -H "Authorization: Bearer $t" "$@" \
    -d "$(python3 -c 'import json,sys;print(json.dumps({"message":sys.argv[1],"contextId":sys.argv[2]}))' "$m" "$c")" |
  python3 -c 'import json,sys
l=sys.stdin.read().rsplit("\n",1); code=l[-1]
try: d=json.loads(l[0]); r=d.get("response") or ("ERROR:"+str(d.get("error")))
except Exception: r="<non-json>"
print(code, r.replace("\n"," ")[:300])'; }
has() { echo "$1" | grep -qi "$2" && echo yes || echo no; }
# a2a <token> <contextId> <message> -> prints "<http code> <reply text>" via A2A message/send
a2a() { local t=$1 c=$2 m=$3
  ks exec $PROBE -n default -- curl -s -m 180 -w '\n%{http_code}' -X POST "$GW/$AGENT/" \
    -H 'Content-Type: application/json' -H "Authorization: Bearer $t" \
    -d "$(python3 -c 'import json,sys,uuid;print(json.dumps({"jsonrpc":"2.0","id":"1","method":"message/send","params":{"message":{"role":"user","messageId":str(uuid.uuid4()),"contextId":sys.argv[2],"parts":[{"kind":"text","text":sys.argv[1]}]}}}))' "$m" "$c")" |
  python3 -c 'import json,sys
l=sys.stdin.read().rsplit("\n",1); code=l[-1]
try:
    d=json.loads(l[0]); r=d.get("result") or {}
    texts=[p.get("text","") for a in (r.get("artifacts") or []) for p in a.get("parts",[])]
    texts+= [p.get("text","") for p in (r.get("parts") or [])]
    out="".join(t for t in texts if t) or ("ERROR:"+json.dumps(d.get("error")) if d.get("error") else "<no text>")
except Exception: out="<non-json>"
print(code, out.replace("\n"," ")[:300])'; }

# rpc <token> <method> <json params> -> raw JSON-RPC response body
rpc() { ks exec $PROBE -n default -- curl -s -m 120 -X POST "$GW/$AGENT/" -H 'Content-Type: application/json' -H "Authorization: Bearer $1" \
    -d "$(python3 -c 'import json,sys;print(json.dumps({"jsonrpc":"2.0","id":"1","method":sys.argv[1],"params":json.loads(sys.argv[2])}))' "$2" "$3")"; }
if [ "$PHASE" = write ]; then
  START=$(date -u +%Y-%m-%dT%H:%M:%SZ); RUN=$(date +%s)
  printf 'START=%s\nRUN=%s\n' "$START" "$RUN" > "$STATE"
  echo "=== provisioning ==="
  row P1 "Memory Ready, region from cluster (none in the example)" "True $CLUSTER_REGION" \
    "$(ks get memories.bedrockagentcore.aws.upbound.io $MEM -o jsonpath='{.status.conditions[?(@.type=="Ready")].status} {.spec.forProvider.region}')"
  row P2 "MemoryStrategy resources Ready (semantic, userPreference)" "True True" \
    "$(ks get memorystrategies.bedrockagentcore.aws.upbound.io $MEM-semantic $MEM-userpreference -o jsonpath='{range .items[*]}{.status.conditions[?(@.type=="Ready")].status} {end}' | xargs)"
  row P3 "AWS GetMemory lists both strategies with per-actor namespaces" "/facts/{actorId}/,/preferences/{actorId}/" \
    "$(aws bedrock-agentcore-control get-memory --memory-id "$MEMID" --region "$REGION" --query 'memory.strategies[].namespaces[]' --output json | python3 -c 'import json,sys;print(",".join(sorted(json.load(sys.stdin))))')"
  ENV=$(ks get pods -n $NS -l app.kubernetes.io/name=$AGENT -o json | python3 -c 'import json,sys
p=[x for x in json.load(sys.stdin)["items"] if x["status"].get("phase")=="Running"]
e={v["name"]:v.get("value") for v in p[0]["spec"]["containers"][0]["env"]} if p else {}
print(json.dumps({"n":len(p),"prov":e.get("MEMORY_PROVIDER"),"cfg":e.get("MEMORY_CONFIG"),"region":e.get("AWS_REGION")}))')
  row P4 "agent pod gets the real memoryId via KubeVela inputs" "$MEMID" "$(echo "$ENV" | python3 -c 'import json,sys;print(json.loads(json.load(sys.stdin)["cfg"] or "{}").get("memoryId"))')"
  row P5 "agent pod region comes from aws-service-identity" "$CLUSTER_REGION" "$(echo "$ENV" | python3 -c 'import json,sys;print(json.load(sys.stdin)["region"])')"
  STRAT_READY=$(ks get memorystrategies.bedrockagentcore.aws.upbound.io $MEM-semantic $MEM-userpreference -o json | python3 -c 'import json,sys
t=[c["lastTransitionTime"] for i in json.load(sys.stdin)["items"] for c in i["status"]["conditions"] if c["type"]=="Ready" and c["status"]=="True"]
print(max(t) if len(t)==2 else "")')
  POD_CREATED=$(ks get pods -n $NS -l app.kubernetes.io/name=$AGENT -o jsonpath='{.items[0].metadata.creationTimestamp}')
  row P6 "agent pod created only after both strategies were Ready (healthPolicy)" yes "$([ -n "$STRAT_READY" ] && [[ "$POD_CREATED" > "$STRAT_READY" ]] && echo yes || echo no)"
  echo "      strategies Ready at $STRAT_READY, pod created at $POD_CREATED"
  POL=$(ks get policies.iam.aws.upbound.io -o json | python3 -c 'import json,sys
for i in json.load(sys.stdin)["items"]:
    if i["metadata"]["name"].endswith("-agentcore-short-memory-iam-policy"): print(i["spec"]["forProvider"]["policy"])')
  row P7 "memory IAM policy: AgentCore actions only, no bedrock:InvokeModel" "yes no" "$(yn "$(echo "$POL" | grep RetrieveMemoryRecords)") $(yn "$(echo "$POL" | grep InvokeModel)")"
  echo "=== no caller identity (bypassing the gateway) ==="
  ks exec $PROBE -n default -- curl -s -m 120 -o /dev/null -X POST "http://$AGENT-stable.$NS.svc.cluster.local:8083/chat" \
    -H 'Content-Type: application/json' -d "{\"message\":\"Say hi.\",\"contextId\":\"nohdr$RUN\"}"
  NOLOG=$(ks logs -n $NS -l app.kubernetes.io/name=$AGENT --since-time="$START" --tail=-1 --max-log-requests=10 2>/dev/null)
  row N1 "request without X-Forwarded-User gets no memory (fail closed)" yes "$(echo "$NOLOG" | grep -q "memory is off for session 'nohdr$RUN'" && echo yes || echo no)"
  row N2 "and no session manager was built for it" no "$(echo "$NOLOG" | grep -q "session manager created.*session=nohdr$RUN" && echo yes || echo no)"
  # Requests without an identity are served (not cached: that part is unit-tested,
  # tests/unit/test_session_isolation.py and test_a2a_isolation.py). /chat three times
  # and A2A once; A2A errors also come back as HTTP 200, so check for a JSON-RPC result.
  NH=""; for i in 1 2 3; do
    NH="$NH $(ks exec $PROBE -n default -- curl -s -m 120 -o /dev/null -w '%{http_code}' -X POST "http://$AGENT-stable.$NS.svc.cluster.local:8083/chat" \
      -H 'Content-Type: application/json' -d "{\"message\":\"Say hi.\",\"contextId\":\"nohdr$RUN\"}")"; done
  NHA=$(ks exec $PROBE -n default -- curl -s -m 120 -X POST "http://$AGENT-stable.$NS.svc.cluster.local:8083/" \
      -H 'Content-Type: application/json' -d "{\"jsonrpc\":\"2.0\",\"id\":\"1\",\"method\":\"message/send\",\"params\":{\"message\":{\"role\":\"user\",\"messageId\":\"m$RUN\",\"contextId\":\"nohdr$RUN\",\"parts\":[{\"kind\":\"text\",\"text\":\"Say hi.\"}]}}}" |
    python3 -c 'import json,sys
try: d=json.load(sys.stdin); print("result" if "result" in d and "error" not in d else "error")
except Exception: print("non-json")')
  row N3 "requests without identity are served (/chat x3 HTTP, A2A result)" "200 200 200 result" "${NH# } $NHA"
  echo "=== parameter validation at admission (server dry-run, as ArgoCD applies) ==="
  # adm <component name> <properties> -> "accepted", "rejected:<constraint>" when the
  # KubeVela webhook reports a CUE constraint, else "other: <error>" (RBAC, webhook
  # down, missing CRD), which no row expects.
  adm() { printf 'apiVersion: core.oam.dev/v1beta1\nkind: Application\nmetadata: {name: ltm-param-probe, namespace: %s}\nspec:\n  components:\n  - name: %s\n    type: agentcore-memory\n    properties: %s\n' "$NS" "$1" "$2" |
    ks apply --dry-run=server -f - 2>&1 | python3 -c 'import sys
o=sys.stdin.read()
if "created (server dry run)" in o: print("accepted")
elif "conflicting values" in o: print("rejected:conflict")
elif "out of bound" in o: print("rejected:bound")
else: print("other:", o.strip().splitlines()[-1][:80] if o.strip() else "empty")'; }
  row V1 "memoryName cannot be overridden (IAM scope stays on own memory)" rejected:conflict "$(adm probe '{memoryName: agents_agentcore_short_memory}')"
  row V2 "eventExpiryDuration 2 and 400 are rejected" "rejected:bound rejected:bound" "$(adm probe '{eventExpiryDuration: 2}') $(adm probe '{eventExpiryDuration: 400}')"
  row V3 "default and in-range expiry are accepted" "accepted accepted" "$(adm probe '{}') $(adm probe '{eventExpiryDuration: 365}')"
  row V4 "namespace+component over 48 chars still gets a valid default name" accepted "$(adm a-component-name-long-enough-to-exceed-the-limit '{}')"
  echo "=== short-term (user1, session A) ==="
  R=$(chat "$U1" "ltmA$RUN" "Remember this about me: my favourite colour is teal and I prefer answers in French.")
  row S1 "user1 states facts: HTTP 200, no error" "200 no" "$(echo "$R" | cut -d' ' -f1) $(has "$R" 'ERROR:')"
  R=$(chat "$U1" "ltmA$RUN" "What is my favourite colour? Answer in English, one word.")
  echo "      S2 reply: $R"
  row S2 "same session recalls (short-term)" "yes" "$(echo "$R" | grep -qiE 'teal|sarcelle|canard' && echo yes || echo no)"
  LOG=$(ks logs -n $NS -l app.kubernetes.io/name=$AGENT --since-time="$START" --tail=-1 --max-log-requests=10 2>/dev/null)
  # Discovery is cached per process, so look at the pod's whole log, not just this run.
  PODLOG=$(ks logs -n $NS -l app.kubernetes.io/name=$AGENT --tail=-1 --max-log-requests=10 2>/dev/null)
  row S3 "agent logs long-term namespaces discovered (once per pod)" "yes" "$(yn "$(echo "$PODLOG" | grep 'long-term namespaces' | grep -F '/facts/{actorId}/')")"
  row S4 "session manager actor = user1 sub, long_term=True" "yes" "$(yn "$(echo "$LOG" | grep "session=ltmA$RUN" | grep "actor=$S1" | grep 'long_term=True')")"
  row S5 "AgentCore stored the events under user1's actor" "yes" \
    "$(aws bedrock-agentcore list-sessions --memory-id "$MEMID" --actor-id "$S1" --region "$REGION" --output json | grep -q "ltmA$RUN" && echo yes || echo no)"
fi

if [ "$PHASE" = read ]; then
  [ -f "$STATE" ] || { echo "no $STATE: run PHASE=write first (same STATE)"; exit 1; }
  . "$STATE"
  echo "=== long-term extraction ==="
  recs() { aws bedrock-agentcore list-memory-records --memory-id "$MEMID" --namespace "$1" --region "$REGION" --query 'length(memoryRecordSummaries)' --output text 2>&1; }
  N1F=$(recs "/facts/$S1/"); N1P=$(recs "/preferences/$S1/"); N2F=$(recs "/facts/$S2/"); N2P=$(recs "/preferences/$S2/")
  echo "      records: user1 facts=$N1F prefs=$N1P | user2 facts=$N2F prefs=$N2P"
  row L1 "records extracted under user1's namespaces" "yes" "$( [ "$N1F" -gt 0 ] 2>/dev/null || [ "$N1P" -gt 0 ] 2>/dev/null && echo yes || echo no)"
  row L2 "no records under user2's namespaces" "0 0" "$N2F $N2P"
  echo "=== recall across sessions ==="
  R=$(chat "$U1" "ltmB$RUN" "What is my favourite colour? Answer in English, one word.")
  # The colour may come back in French: user1 also stated a French-answer preference,
  # which is the second long-term record. R1 checks the fact, R1b the preference.
  row R1 "user1, NEW session B, recalls colour (teal / sarcelle)" "yes" "$(echo "$R" | grep -qiE 'teal|sarcelle|canard' && echo yes || echo no)"
  R1B=$(chat "$U1" "ltmE$RUN" "Give me one sentence about the weather in general.")
  row R1b "user1, NEW session E, answers in French (preference recalled)" "yes" "$(echo "$R1B" | grep -qiE '\b(le|la|les|est|il|temps|météo)\b' && echo yes || echo no)"
  echo "      user1 B: $R"; echo "      user1 E: $R1B"
  R=$(chat "$U2" "ltmC$RUN" "What is my favourite colour? If you do not know, say UNKNOWN.")
  row R2 "user2, new session, does NOT get user1's colour" "no" "$(echo "$R" | grep -qiE 'teal|sarcelle|canard' && echo yes || echo no)"; echo "      user2:   $R"
  R=$(chat "$U2" "ltmD$RUN" "What is my favourite colour? If you do not know, say UNKNOWN." -H "X-Forwarded-User: $S1")
  row R3 "user2 forging X-Forwarded-User=user1 still does NOT get it" "no" "$(echo "$R" | grep -qiE 'teal|sarcelle|canard' && echo yes || echo no)"; echo "      forged:  $R"
  R=$(chat "$U2" "ltmA$RUN" "What is my favourite colour? If you do not know, say UNKNOWN.")
  row R4 "user2 reusing user1's contextId does NOT see user1's session" "no" "$(echo "$R" | grep -qiE 'teal|sarcelle|canard' && echo yes || echo no)"
  echo "=== A2A JSON-RPC route ==="
  CX="a2a$(date +%s)"
  R=$(a2a "$U1" "$CX" "What is my favourite colour? Answer in English, one word.")
  row A1 "user1, A2A new context, recalls colour (per-caller memory on A2A)" "yes" "$(echo "$R" | grep -qiE 'teal|sarcelle|canard' && echo yes || echo no)"; echo "      user1 A2A: $R"
  R=$(a2a "$U2" "$CX" "What is my favourite colour? If you do not know, say UNKNOWN.")
  row A2 "user2 reusing user1's A2A contextId does NOT get user1's memory" "no" "$(echo "$R" | grep -qiE 'teal|sarcelle|canard' && echo yes || echo no)"; echo "      user2 A2A: $R"
  echo "=== A2A tasks/* isolation ==="
  TX="a2at$(date +%s)"
  TID=$(rpc "$U1" message/send "$(python3 -c 'import json,sys,uuid;print(json.dumps({"message":{"role":"user","messageId":str(uuid.uuid4()),"contextId":sys.argv[1],"parts":[{"kind":"text","text":"Say hi."}]}}))' "$TX")" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("result",{}).get("id",""))')
  echo "      task id: $TID"
  # Non-blocking send: the task finishes after the HTTP response, and is saved by a2a-sdk
  # in the background. Checked after A3-A7 so it has time to complete.
  NB=$(rpc "$U1" message/send "$(python3 -c 'import json,sys,uuid;print(json.dumps({"configuration":{"blocking":False},"message":{"role":"user","messageId":str(uuid.uuid4()),"contextId":sys.argv[1]+"nb","parts":[{"kind":"text","text":"Say hi in five words."}]}}))' "$TX")" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("result",{}).get("id",""))')
  # Streamed send: saves happen while the response streams.
  ST=$(ks exec $PROBE -n default -- curl -s -N -m 120 -X POST "$GW/$AGENT/" -H 'Content-Type: application/json' -H "Authorization: Bearer $U1" \
    -d "$(python3 -c 'import json,sys,uuid;print(json.dumps({"jsonrpc":"2.0","id":"1","method":"message/stream","params":{"message":{"role":"user","messageId":str(uuid.uuid4()),"contextId":sys.argv[1]+"st","parts":[{"kind":"text","text":"Say hi."}]}}}))' "$TX")" |
    python3 -c 'import json,sys
ids=set()
for l in sys.stdin:
    l=l.strip()
    if l.startswith("data:"):
        try: r=json.loads(l[5:]).get("result",{})
        except Exception: continue
        i=r.get("taskId") or (r.get("id") if r.get("kind")=="task" else None)
        if i: ids.add(i)
print(next(iter(ids),""))')
  echo "      non-blocking task: $NB   streamed task: $ST"
  row A3 "user1 tasks/get on own task returns it" "$TID" "$(rpc "$U1" tasks/get "{\"id\":\"$TID\"}" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("result",{}).get("id",""))')"
  row A4 "user2 tasks/get on user1's task id is task-not-found (-32001)" "-32001" "$(rpc "$U2" tasks/get "{\"id\":\"$TID\"}" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("error",{}).get("code","NONE"))')"
  row A5 "user2 tasks/cancel on user1's task id is task-not-found (-32001)" "-32001" "$(rpc "$U2" tasks/cancel "{\"id\":\"$TID\"}" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("error",{}).get("code","NONE"))')"
  row A6 "user1 tasks/cancel on own finished task reaches the task (-32002 not cancelable)" "-32002" "$(rpc "$U1" tasks/cancel "{\"id\":\"$TID\"}" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("error",{}).get("code","NONE"))')"
  U1b=$(tok user1)
  row A7 "user1 tasks/get on own task with a REFRESHED token still returns it" "$TID" "$(rpc "$U1b" tasks/get "{\"id\":\"$TID\"}" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("result",{}).get("id",""))')"
  state() { python3 -c 'import json,sys;d=json.load(sys.stdin);print(d.get("result",{}).get("status",{}).get("state") or d.get("error",{}).get("code","NONE"))'; }
  row A8 "non-blocking task: owner reads it after it completed in the background" completed "$(rpc "$U1" tasks/get "{\"id\":\"$NB\"}" | state)"
  row A9 "non-blocking task: another caller gets task-not-found" -32001 "$(rpc "$U2" tasks/get "{\"id\":\"$NB\"}" | state)"
  row A10 "streamed task: owner reads it" completed "$(rpc "$U1" tasks/get "{\"id\":\"$ST\"}" | state)"
  row A11 "streamed task: another caller gets task-not-found" -32001 "$(rpc "$U2" tasks/get "{\"id\":\"$ST\"}" | state)"
  echo "=== regression and health ==="
  ASST=$(ks exec $PROBE -n default -- curl -s -m 120 -o /dev/null -w '%{http_code}' -X POST "$GW/oap-assistant-a/chat" -H 'Content-Type: application/json' -H "Authorization: Bearer $U1" -d '{"message":"What time is it in UTC? Use your tool."}')
  row H1 "memory-less assistant (oap-assistant-a) still answers" "200" "$ASST"
  GWLOG=$(ks logs -n agentgateway-system -l app.kubernetes.io/name=agentgateway-proxy --since-time="$START" --tail=-1 2>/dev/null)
  row H2 "no 5xx on the memory agent route during the run" "0" "$(echo "$GWLOG" | grep "route=$NS/$AGENT " | grep -cE 'http.status=5[0-9]{2}')"
  row H3 "agent pods have not restarted" "0" "$(ks get pods -n $NS -l app.kubernetes.io/name=$AGENT -o jsonpath='{range .items[*]}{.status.containerStatuses[0].restartCount}{"\n"}{end}' | awk '{s+=$1} END{print s+0}')"
  ks delete pod $PROBE -n default --ignore-not-found >/dev/null 2>&1
fi
echo "FAILED ROWS: $FAILS"; exit $FAILS
