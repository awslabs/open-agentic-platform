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
│       ├── sandbox.cue
│       └── agentcore-memory.cue
└── examples/
    ├── example-agent-minimal.yaml          Minimal agent
    ├── example-agent-simple.yaml           Agent + LLM gateway
    ├── example-agent-sandbox.yaml          Agent in a hardware-isolated microVM (sandbox: true)
    ├── example-agent-sandbox-confidential.yaml  ... with the node operator outside the trust boundary
    ├── example-agent-tools-isolation.yaml        Shared agent, per-session TOOL sandbox (isolation: tools)
    ├── example-agent-session-isolation.yaml     One isolated sandbox per user session (isolation: session)
    ├── example-sandbox-isolated.yaml        Per-session sandbox, isolated mode (type: sandbox)
    ├── example-sandbox-confidential.yaml    Per-session sandbox, confidential + attestation
    ├── example-agent-with-mcp.yaml          Agent integrated with an MCP server
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
`sub`, so one caller's memories are never retrieved for another. A request without that
header did not come through the gateway, and the agent gives it no memory at all rather
than pooling such callers together. The agent reads the strategies from the memory itself
(`GetMemory`, refreshed every 5 minutes), so adding a strategy needs no agent change and no
rollout. Sessions that are already open keep the strategies they started with; new
sessions use the current ones. Retrieval tuning: `retrievalTopK` and
`retrievalRelevanceScore` in `memory.config`, as strings (`"5"`, `"0.3"`); defaults 10 and
0.2, and an unparsable value falls back to the default with a warning.

The `summary` namespace includes `{sessionId}`, so summaries are recalled within the
session that produced them, not across sessions. Use `semantic` or `userPreference` for
recall across sessions.

The `agentcore-memory` component reports healthy only once the memory and all its
strategies exist, so an agent that `dependsOn` it starts with every strategy in place.
Removing a strategy from the list deletes it in AgentCore.

Upgrading from an earlier version of this component renames the Memory object (to
the form described under "Upgrading an existing install" below), so the AWS memory is recreated with a new
memoryId and its stored events are lost. An Application that set `memoryName` must drop it: the field
is no longer accepted (see below), and the memory is recreated under the derived name.

The component's IAM Policy object is named after the policy's **AWS** name, and
`aws-service-identity`'s `accessFor` finds it by that name (`policyArnRef`; appmod's trait
computes the same name). The AWS name is the Policy's external name
(`crossplane.io/external-name`; the provider's schema has no `forProvider.name` for an IAM
Policy, so that field is ignored). IAM policy names are unique per AWS account, so it
carries the install, by the same rule as the
platform's `component-iam-policy` trait (appmod), byte for byte:

```
<first 100 chars of "<cluster>-<appName>-<component>">-<8 hex>-iam-policy      (<= 128)
8 hex = sha256("<region>/<cluster>/<namespace>/<appName>/<component>")[:8]
```

In the name, `<cluster>` is lowercased and `_` becomes `-`, so the object name is a valid Kubernetes
name for any EKS cluster name (EKS allows uppercase and `_`). The hash input keeps the cluster name
as given. appmod's trait applies the same mapping.

`<cluster>` and `<region>` are the chart's `global.clusterName` and `global.awsRegion` (the
cluster secret's `aws_cluster_name` and `aws_region` annotations, which the platform already
passes). The hash is always present. Two installs in one account never share a policy, nor do
two regions that reuse a cluster name. The name begins with the cluster name, so a sweep that
matches the resource prefix reclaims it.

The same rule applies to the IAM policy emitted by `agentcore-browser`,
`agentcore-code-interpreter` and `eks-read-access`.
The browser and interpreter names themselves (`browserName`, `interpreterName`) are chosen by
the developer and are also unique per AWS account: two installs in one account that deploy the
same Application must give them different names, because the platform does not derive them.

**The cluster name is required.** Every name above depends on `global.clusterName`. When it
is empty (the chart rendered standalone, or by a platform that does not pass the cluster
secret's `aws_cluster_name`), `agentcore-memory`, `agentcore-browser`,
`agentcore-code-interpreter` and `eks-read-access` fail the render with
`_installIdentity: conflicting values "global.clusterName is empty: ..."` instead of falling
back to names that collide between installs (#92). appmod's `aws-service-identity` and
`component-iam-policy` traits compute the same names from the same value, so the hub's
KubeVela chart must receive it too: its values come from the hub's appmod platform
revision, whose addon registry passes `aws_cluster_name` from v0.4.0-rc1 on.

`scripts/check-iam-names.sh <appmod-blueprints checkout>` keeps the two repos in step. It
renders these components and appmod's traits and aiops-agent chart for a matrix of
installs, offline, and checks that every attachment finds its Policy object, that the
names match an independent implementation of the rule, that no name is shared between
installs, that label values stay within 63 characters, and that an empty cluster name is
refused. Run it whenever the rule changes on either side.

The AWS memory name is derived, not set, and is one-to-one with the install, namespace
and component. AgentCore requires it to be unique within the AWS account, so it carries
the install too: `M<first 30 characters of <cluster>_<namespace>_<component>>_<16-character
hash>`, with `-` replaced by `_` and the hash taken over `cluster/namespace/component`.
It is at most 48 characters and starts with a letter, as AgentCore requires.
`memoryName` cannot be overridden: the IAM policy grants `memory/<memoryName>-*`, so a
chosen or colliding name could reach another namespace's memory. `eventExpiryDuration`
must be 3 to 365 days. KubeVela rejects other values when the Application is applied.

**Upgrading an existing install.** Upgrading from a version that derived the names without
the install changes the AWS memory name of
every `agentcore-memory` component, and the AWS policy name of every `agentcore-memory`,
`agentcore-browser`, `agentcore-code-interpreter` and `eks-read-access` component.

- Memory. A memory cannot be renamed (the name is `RequiresReplace` in the provider, and the
  Crossplane provider refuses replace plans), so the component never changes the name of an
  existing Memory object. The Memory object name follows the AWS name
  (`<namespace>.<component>.<8 hex of sha256(memoryName)>`), and so do its MemoryStrategy
  objects. A new
  memory name is therefore a new object: KubeVela creates the new Memory and strategies and
  garbage-collects the old ones, which deletes the old AWS memory. The agent receives the
  new memoryId through the component output and rolls. The old memory's stored events are
  lost: plan the upgrade as a replace.
- IAM policy. The Policy object is renamed with its AWS name, so KubeVela creates a new
  object and policy and garbage-collects the old object, which deletes the old AWS policy
  once it is detached. A policy still attached by another cluster's old attachments stays
  until that cluster upgrades. The new object has no ARN until its policy exists, so an
  attachment cannot latch the old policy's ARN. Merge with appmod's matching trait change:
  the trait looks the object up by the new name.
- Attachments. A `RolePolicyAttachment` resolves `policyArnRef` once and keeps that ARN
  (`policy_arn` is immutable), so an existing attachment stays on the old AWS policy. The
  role reaches the new policy only through a new attachment. The platform's
  `aws-service-identity` trait creates new attachments when it moves to per-cluster role
  names (appmod, per-cluster IAM role and policy names), so deploy this version with, or
  after, that trait version. With an older trait the role keeps the old policy, which grants
  only the old memory name: the agent is denied access to the new memory.

Run the agent with **one replica** (the `agent` component's default), with or without memory,
and do not add an `hpa` or `cpuscaler` trait to it. With memory, AgentCore restores the
conversation on any pod, but A2A tasks and contexts live in the memory of the pod that served them, so
a follow-up that lands on another pod gets "not found".

Requests that reach the agent's Service without passing the gateway, such as an
incident bridge posting to it directly, carry no `X-Forwarded-User`, so they get no
memory and no agent kept between turns. Route such a caller through the gateway with its
own ServiceAccount token if it needs either. The A2A tasks such requests create can never
be read back, so the agent keeps only the newest `MAX_UNOWNED_TASKS` of them (agent env,
default `100`).

The `X-Forwarded-User` header is trustworthy only on traffic through the gateway. A
workload that calls an agent's Service directly can set it to any caller; restrict agent
ingress to `agentgateway-system` with a NetworkPolicy if other workloads share the
cluster (enforcing this by default is tracked in #86).

Long-term records are extracted asynchronously by AgentCore after a conversation, so a
fact told in one session is recallable in a new session after extraction completes, not
immediately.

Live check: `scripts/test-memory-matrix.sh` (provisioning, fail-closed, per-caller
recall, A2A context and task isolation). Run `PHASE=write`, wait for extraction, then
`PHASE=read`. The gateway side is `scripts/test-identity-matrix.sh`.

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
