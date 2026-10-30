# Demo: per-caller long-term memory for agents

What the audience sees, in about ten minutes:

1. A developer opts an agent into long-term memory with one line of OAM.
2. The agent remembers a user's fact and preference across sessions.
3. Another user cannot see those memories, even by reusing the session id or forging
   the identity header.

The demo runs against the reference example,
[`platform/oam/examples/example-agent-agentcore-memory.yaml`](../../platform/oam/examples/example-agent-agentcore-memory.yaml),
on a spoke. The commands use the reference install's names (hub `peeks-hub`, spoke
`oap-dev`, region `us-west-2`). Change the three variables in step 0 if yours differ.

## Prerequisites

- AWS credentials that can read EKS on both clusters and call
  `bedrock-agentcore:ListMemoryRecords`.
- `kubectl`, `aws`, `curl` and `jq`.
- Keycloak users `user1` and `user2` in realm `platform`, with the shared demo password in
  the hub's `keycloak-config` secret (both exist in the reference install).
- The example Application on the spoke. Check, and apply it if it's missing:

  ```bash
  kubectl --context oap-dev get application.core.oam.dev agent-with-agentcore-memory-app -n agents \
    || kubectl --context oap-dev apply -f platform/oam/examples/example-agent-agentcore-memory.yaml
  ```

  The memory and its strategies take a few minutes to become Ready the first time.

Run everything from the repository root.

## 0. Setup

**Terminal 2.** Forward the spoke's gateway and leave it running. The spoke gateway has
no public endpoint, so the demo calls it through this port-forward:

```bash
aws eks update-kubeconfig --name oap-dev --region us-west-2 --alias oap-dev
kubectl --context oap-dev port-forward -n agentgateway-system svc/agentgateway-proxy 18084:8080
```

**Terminal 1.** Load the values the demo uses. Each one is read from the clusters:

```bash
HUB=peeks-hub; SPOKE=oap-dev; REGION=us-west-2
aws eks update-kubeconfig --name $HUB --region $REGION --alias $HUB
aws eks update-kubeconfig --name $SPOKE --region $REGION --alias $SPOKE

# Keycloak domain and the demo users' password, from the hub
DOMAIN=$(kubectl --context $HUB get secret -n argocd -l argocd.argoproj.io/secret-type=cluster \
  -o jsonpath='{range .items[*]}{.metadata.annotations.ingress_domain_name}{"\n"}{end}' | grep -v '^$' | head -1)
UPW=$(kubectl --context $HUB get secret keycloak-config -n keycloak -o jsonpath='{.data.USER_PASSWORD}' | base64 -d)

# The AgentCore memory the example created
MEMID=$(kubectl --context $SPOKE get memories.bedrockagentcore.aws.upbound.io agents.agentcore-short-memory \
  -o jsonpath='{.metadata.annotations.crossplane\.io/external-name}')

# Tokens for both users, and user1's subject (the memory actor)
tok() { curl -s -X POST "https://$DOMAIN/keycloak/realms/platform/protocol/openid-connect/token" \
  -d client_id=mcp-client -d username=$1 --data-urlencode "password=$UPW" -d grant_type=password | jq -r .access_token; }
U1=$(tok user1); U2=$(tok user2)
S1=$(echo "$U1" | cut -d. -f2 | tr '_-' '/+' | awk '{l=length($0)%4; if(l==2)$0=$0"=="; else if(l==3)$0=$0"="; print}' | base64 -d | jq -r .sub)

# A codename unique to this run, so a recall can't come from an earlier rehearsal
CODE=cobalt-$RANDOM

# chat <token> <contextId> <message> [extra curl args]
chat() { curl -s -X POST http://localhost:18084/my-agent/chat -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $1" "${@:4}" -d "$(jq -cn --arg m "$3" --arg c "$2" '{message:$m,contextId:$c}')" | jq -r .response; }

echo "domain=$DOMAIN memory=$MEMID user1=$S1 codename=$CODE"
```

The tokens are valid for an hour. If a step prints `null`, rerun the `U1=` and `U2=` line.

## 1. One line opts in

**Say:** the developer writes this. Long-term memory is the `strategies` line. There is
no region, account or IAM anywhere in it.

```bash
sed -n '/^spec:/,$p' platform/oam/examples/example-agent-agentcore-memory.yaml | grep -v '^\s*#'
```

## 2. What the platform provisioned

**Say:** Crossplane created the AgentCore memory and one strategy per line item.

```bash
kubectl --context $SPOKE get memories.bedrockagentcore.aws.upbound.io,memorystrategies.bedrockagentcore.aws.upbound.io \
  | grep -E 'NAME|agents.agentcore-short-memory'
```

Expected: the memory and both strategies (`semantic`, `userpreference`) are `READY True`.

## 3. user1 tells the agent something

**Say:** the request carries no user id. The gateway validates the login and passes the
caller's identity to the agent.

```bash
chat $U1 demo-a "Remember this about me: my team project codename is $CODE, and I prefer answers in French."
```

## 4. The same session remembers (short-term memory)

```bash
chat $U1 demo-a "What is my team project codename? Reply with the codename only."
```

Expected: the codename.

## 5. AWS extracts long-term records

Extraction is asynchronous and usually takes two to five minutes. Rerun until the
codename appears:

```bash
aws bedrock-agentcore list-memory-records --memory-id $MEMID --namespace /facts/$S1/ --region $REGION \
  --query 'memoryRecordSummaries[].content.text' --output text | grep -i $CODE
```

**Say:** the facts are stored under user1's own namespace, `/facts/<user1's id>/`.

The preference is a separate record:

```bash
aws bedrock-agentcore list-memory-records --memory-id $MEMID --namespace /preferences/$S1/ --region $REGION \
  --query 'memoryRecordSummaries[].content.text' --output text
```

## 6. A new session remembers (long-term memory)

**Say:** a brand-new session, and nothing in the request about earlier conversations.

```bash
chat $U1 demo-b "What is my team project codename? Reply with the codename only."
chat $U1 demo-c "Describe the weather today in one sentence."
```

Expected: the codename, then an answer in French. The second shows the preference was
recalled as well.

## 7. Memories are per user

user2 asks in a new session:

```bash
chat $U2 demo-d "What is my team project codename? If you do not know, say UNKNOWN."
```

user2 reuses user1's session id:

```bash
chat $U2 demo-a "What is my team project codename? If you do not know, say UNKNOWN."
```

user2 forges the identity header to claim to be user1. The gateway overwrites it with
the real caller:

```bash
chat $U2 demo-e "What is my team project codename? If you do not know, say UNKNOWN." -H "X-Forwarded-User: $S1"
```

Expected for all three: `UNKNOWN`, or the agent saying it doesn't know.

## Cleanup

Stop the port-forward in terminal 2 with Ctrl-C. The demo's records stay in AgentCore
until the memory's event expiry (30 days in the example).

## Troubleshooting

| Symptom | Cause |
|---|---|
| `chat` prints `null` | Expired token; rerun the `U1=`/`U2=` line. Or the port-forward stopped. |
| Step 5 never shows the codename | Extraction hasn't run yet; wait a little longer. Check the strategies are Ready (step 2). |
| Step 6 doesn't recall | Step 5 wasn't complete. Long-term recall needs the extracted record. |
| Every call returns HTTP 401 | The token is for another Keycloak realm or domain; check `DOMAIN`. |

## How it works

- The gateway's `caller-identity-policy` sets `X-Forwarded-User` to the validated
  token's `sub`, replacing any value the client sent.
- The agent uses that subject as the AgentCore actor, and the strategy namespaces embed
  it (`/facts/{actorId}/`, `/preferences/{actorId}/`), so each user's records are
  separate.
- On every user message, the AgentCore session manager searches those namespaces with
  the message text and adds the matches to the prompt.
- `contextId` in the request body is the session id. It selects the conversation
  (short-term memory) and plays no part in long-term recall.

More detail: [`platform/oam/README.md`](../../platform/oam/README.md#agent-memory-agentcore).
