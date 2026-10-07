# OAM Components for Agents and MCP Servers

KubeVela ComponentDefinitions and Application examples for deploying AI agents and MCP servers with Argo Rollouts on Kubernetes.

For architectural decisions and rationale, see **[DESIGN.md](DESIGN.md)**.

## Components

| Component | File | Description |
|---|---|---|
| `agent` | `definitions/components/agent.cue` | A2A agent with blue-green deployment, pluggable memory, and AgentGateway registration |
| `mcp-server` | `definitions/components/mcp-server.cue` | MCP server with blue-green deployment and AgentGateway backend registration |
| `agentcore-memory` | `definitions/components/agentcore-memory.cue` | Bedrock AgentCore Memory provisioned via the `crossplane-agentcore` Composition |

These ComponentDefinitions are packaged for cluster registration by the `oam-agent-components` Helm chart at `gitops/addons/charts/oam-agent-components/`.

## Layout

```
platform/oam/
├── DESIGN.md                      Architectural decisions
├── README.md                      This file
├── generate.sh                    Regenerate ComponentDefinition YAMLs from CUE
├── definitions/
│   └── components/
│       ├── agent.cue
│       ├── mcp-server.cue
│       └── agentcore-memory.cue
└── examples/
    ├── example-agent-minimal.yaml          Minimal agent
    ├── example-agent-simple.yaml           Agent + LLM gateway
    ├── example-agent-sandbox.yaml          Agent isolated in a Kata microVM (sandbox: true)
    ├── example-agent-with-mcp.yaml         Agent integrated with an MCP server
    ├── example-agent-agentcore-memory.yaml Agent backed by Bedrock AgentCore Memory
    ├── example-agent-milvus-memory.yaml    Agent backed by Milvus vector store
    ├── example-mcp-local.yaml              MCP server
    ├── test-agentcore-memory.yaml          Standalone AgentCore Memory test
    ├── agentgateway-backend.yaml           AgentGateway backend manifest
    ├── agentgateway-httproute.yaml         AgentGateway HTTPRoute manifest
    ├── kgateway-session-affinity.yaml      kgateway BackendConfigPolicy for sticky sessions
    └── register-agent-with-gateway.yaml    Custom HTTPRoute examples
```

## Regenerating ComponentDefinition YAMLs

The Helm chart at `gitops/addons/charts/oam-agent-components/templates/` ships pre-generated YAMLs. To regenerate from CUE:

```bash
cd platform/oam
./generate.sh
```

Output lands under `gitops/addons/charts/oam-agent-components/templates/`.

## Image notes

The example files use `tiangolo/uvicorn-gunicorn-fastapi:python3.11` as a placeholder image for testing the Argo Rollouts mechanism. For real agent functionality, build a Strands agent image using `applications/strands-agent-base/`.

## Prerequisites

- Kubernetes cluster with:
  - KubeVela installed (provided by the `kubevela` addon from appmod-blueprints)
  - Argo Rollouts installed
  - AWS Load Balancer Controller (for ALB traffic routing, when using ingress)
- AWS resources:
  - Amazon Bedrock access with desired Claude/Anthropic models enabled
  - ECR repository for agent images
  - IAM roles / Pod Identity associations for Bedrock access (when bypassing the LLM gateway)

## Building a Strands agent image

The reference Strands agent app lives at `applications/strands-agent-base/`:

```bash
cd applications/strands-agent-base

# Build for AMD64 (compatible with EKS)
./build.sh

# Or with Podman
./build-podman.sh

# Push to ECR
./build.sh push
```

The build scripts auto-detect AWS account/region and create the ECR repo if needed. See `applications/strands-agent-base/README.md` for full details.

## Pod Identity for Bedrock

### Option 1: LLM Gateway (recommended)

LiteLLM proxy exposes an OpenAI-compatible endpoint and authenticates to Bedrock via its own Pod Identity. Agents talk to the gateway over plain HTTP — no AWS credentials in agent pods.

The `agent` ComponentDefinition uses LLM Gateway by default:

```yaml
modelConfig:
  modelId: claude-sonnet
  llmGatewayUrl: http://litellm-proxy.litellm.svc.cluster.local:4000
  llmGatewayApiKey: sk-1234
```

See `examples/example-agent-simple.yaml` for a complete Application.

### Option 2: Direct Bedrock access via Pod Identity

For agents that call Bedrock directly:

```bash
NAMESPACE=default      # or whatever namespace your agent runs in
SA=weather-agent-sa
CLUSTER=<your-cluster-name>

aws iam create-policy \
  --policy-name strands-agents-bedrock-policy \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
      "Resource": "*"
    }]
  }'

eksctl create podidentityassociation \
  --cluster $CLUSTER \
  --namespace $NAMESPACE \
  --service-account-name $SA \
  --permission-policy-arns arn:aws:iam::${AWS_ACCOUNT_ID}:policy/strands-agents-bedrock-policy \
  --role-name eks-strands-weather-agent
```

## Deploying an agent

Once the `oam-agent-components` chart is installed, ComponentDefinitions are registered and you can apply Applications:

```bash
# Pick an example
kubectl apply -f examples/example-agent-simple.yaml

# Or deploy your own using the agent ComponentDefinition
```

Each agent Application creates:
- Argo Rollout (blue-green by default)
- Stable + preview Services
- Agent Card ConfigMap (for A2A discovery)
- HTTPRoute against `agentgateway-proxy` (when `registerWithGateway: true`)

## Agent memory (AgentCore)

`examples/example-agent-agentcore-memory.yaml` is the reference. The memory ID is the only
wired value: the `agentcore-memory` component publishes it as a KubeVela output, and the
agent takes it as an input with `dependsOn`, so the agent starts after the memory exists.
Neither component takes a region; both use the cluster's.

| Mode | Set on `agentcore-memory` | What the agent gets |
|---|---|---|
| Short-term | nothing (`strategies: []`, default) | the conversation within a session (`contextId`) |
| Long-term | `strategies: [semantic, userPreference, summary]`, any subset | short-term, plus records recalled across sessions |

Strategy namespaces are platform-owned and keyed by the caller:

| Strategy | AgentCore type | Namespace |
|---|---|---|
| `semantic` | `SEMANTIC` | `/facts/{actorId}/` |
| `userPreference` | `USER_PREFERENCE` | `/preferences/{actorId}/` |
| `summary` | `SUMMARIZATION` | `/summaries/{actorId}/{sessionId}/` |

`actorId` is the `X-Forwarded-User` header the gateway sets from the caller's token
`sub`, so one caller's memories are never retrieved for another. Requests that did not
come through the gateway share the actor `anonymous`. The agent reads the strategies from
the memory itself (`GetMemory`, refreshed every 5 minutes), so adding a strategy needs no
agent change and no rollout. Retrieval tuning: `retrievalTopK` and
`retrievalRelevanceScore` in `memory.config` (defaults 10 and 0.2).

Long-term records are extracted asynchronously by AgentCore after a conversation, so a
fact told in one session is recallable in a new session after extraction completes, not
immediately.

## Monitoring rollouts

```bash
NAMESPACE=default      # adjust for your agent

# Watch rollout
kubectl argo rollouts get rollout <agent-name> -n $NAMESPACE --watch

# Promote a paused canary/blue-green
kubectl argo rollouts promote <agent-name> -n $NAMESPACE

# Abort
kubectl argo rollouts abort <agent-name> -n $NAMESPACE

# Restart
kubectl argo rollouts restart <agent-name> -n $NAMESPACE
```

## AgentGateway registration

AgentGateway uses Kubernetes Gateway API to route to agents.

### Automatic registration (default)

The `agent` ComponentDefinition creates an HTTPRoute by default:

```yaml
apiVersion: core.oam.dev/v1beta1
kind: Application
metadata:
  name: my-assistant
spec:
  components:
    - name: assistant
      type: agent
      properties:
        name: assistant
        namespace: agents
        # ... other properties ...

        # Gateway registration (default: true)
        registerWithGateway: true
```

This creates:
- Service with `appProtocol: kgateway.dev/a2a`
- HTTPRoute pointing to `agentgateway-proxy` in `agentgateway-system`
- Default route from `/` to the stable Service

### Manual / custom registration

To disable auto-registration and write your own routes:

```yaml
properties:
  registerWithGateway: false
```

```yaml
# Custom HTTPRoute for path-based routing
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: custom-agent-routes
  namespace: agents
spec:
  parentRefs:
  - name: agentgateway-proxy
    namespace: agentgateway-system
  rules:
  - matches:
    - path: { type: PathPrefix, value: /assistant }
    filters:
    - type: URLRewrite
      urlRewrite:
        path: { type: ReplacePrefixMatch, replacePrefixMatch: / }
    backendRefs:
    - name: assistant-stable
      port: 8083
```

See `examples/register-agent-with-gateway.yaml` for more.

### Calling registered agents

```bash
GATEWAY_URL=$(kubectl get svc -n agentgateway-system agentgateway-proxy \
  -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')

# Default registration (root path)
curl http://${GATEWAY_URL}/.well-known/agent.json
curl -X POST http://${GATEWAY_URL}/message \
  -H 'Content-Type: application/json' \
  -d '{"text": "Hello, assistant!"}'

# Path-based custom routing
curl http://${GATEWAY_URL}/assistant/.well-known/agent.json
```

## Local testing

```bash
NAMESPACE=default
AGENT=weather-agent

# Port-forward to the stable service
kubectl port-forward -n $NAMESPACE svc/${AGENT}-stable 8083:8083

# In another terminal
curl http://localhost:8083/.well-known/agent.json
curl -X POST http://localhost:8083/invoke \
  -H 'Content-Type: application/json' \
  -d '{"prompt": "What is the weather in Seattle?"}'
```

## Customizing deployment strategies

Default is blue-green. To use canary, override `rolloutStrategy` in your Application properties.

### Canary with ALB traffic routing

```yaml
rolloutStrategy:
  canary:
    steps:
      - setWeight: 20
      - pause: {duration: 5m}
      - setWeight: 50
      - pause: {duration: 5m}
    trafficRouting:
      alb:
        ingress: weather-agent-ingress
        rootService: weather-agent-stable
        servicePort: 8083
    dynamicStableScale: true
```

### Canary with Istio

```yaml
rolloutStrategy:
  canary:
    steps:
      - setWeight: 25
      - pause: {duration: 2m}
    trafficRouting:
      istio:
        virtualService:
          name: weather-agent-vsvc
          routes:
            - primary
```

### Blue-green with auto-promotion

```yaml
rolloutStrategy:
  blueGreen:
    autoPromotionEnabled: true
    scaleDownDelaySeconds: 30
```

## Troubleshooting

```bash
NAMESPACE=default

# Rollout status
kubectl describe rollout <agent-name> -n $NAMESPACE

# Pod logs
kubectl logs -n $NAMESPACE -l app.kubernetes.io/name=<agent-name>

# Services
kubectl get svc -n $NAMESPACE -l app.kubernetes.io/name=<agent-name>

# Agent card
kubectl get configmap -n $NAMESPACE <agent-name>-card -o yaml
```

## References

- [Strands Agents Documentation](https://strandsagents.com/latest/documentation/)
- [Argo Rollouts Documentation](https://argo-rollouts.readthedocs.io/)
- [KubeVela OAM Specification](https://kubevela.io/docs/)
- [A2A Protocol Specification](https://github.com/aws/agent-to-agent-protocol)
