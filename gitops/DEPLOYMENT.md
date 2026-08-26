# Agent Platform GitOps Deployment Guide

Deploy the AI agent platform on an EKS cluster using the ArgoCD EKS Capability.

## What Gets Deployed

| Wave | Addon | Source | Namespace |
|------|-------|--------|-----------|
| 3 | litellm (LLM gateway) | Local chart | litellm |
| 4 | crossplane-agentcore | Local chart | crossplane-system |
| 5 | langfuse (LLM tracing + PostgreSQL) | Local chart | langfuse |
| 5 | jaeger (distributed tracing) | Helm: `jaegertracing/jaeger:3.4.1` | jaeger |
| 5 | otel-collector | Local chart | otel |
| 5 | prometheus-operator-crds | Helm: `prometheus-community/prometheus-operator-crds:28.0.1` | monitoring |
| 5 | bifrost (AI gateway) | Helm: `maximhq/bifrost:2.1.16` | bifrost |
| 5 | oam-agent-components (KubeVela ComponentDefinitions) | Local chart | vela-system |
| 7 | gateway-api-crds | Local chart (Job) | agentgateway-system |
| 7 | agentgateway-crds | OCI: `cr.agentgateway.dev/charts/agentgateway-crds:v1.1.0` | agentgateway-system |
| 8 | agentgateway (control plane) | OCI: `cr.agentgateway.dev/charts/agentgateway:v1.1.0` | agentgateway-system |
| 9 | agent-gateway (Gateway + Policies) | Local chart | agent-core-infra |

### AgentCore via Crossplane (wave 4)

The `crossplane-agentcore` chart provisions Bedrock AgentCore resources using Crossplane compositions:

- **Provider**: `provider-aws-bedrockagentcore` (v2.5.3) with its own Pod Identity
- **XRDs**: `AgentCoreMemory`, `AgentCoreBrowser`, `AgentCoreCodeInterpreter`
- **Compositions**: Pipeline mode using `function-patch-and-transform`
- **Claims**: Create Memory, Browser, and Code Interpreter with cluster-unique names (`{clusterName}_{projectName}_{type}`)

Prerequisites: Crossplane must be installed with `provider-family-aws`, `provider-aws-iam`, and `provider-aws-eks` (provided by appmod-blueprints or installed separately).

### OAM Agent Components (wave 5)

The `oam-agent-components` chart registers KubeVela ComponentDefinitions for declaratively deploying agents and MCP servers:

- **`agent`** — A2A agent with blue-green deployment via Argo Rollouts and pluggable memory backends
- **`mcp-server`** — MCP server with blue-green deployment and AgentGateway registration
- **`agentcore-memory`** — Bedrock AgentCore Memory provisioned via the `crossplane-agentcore` Composition

Prerequisite: KubeVela must be installed (provided by appmod-blueprints `kubevela` addon at sync wave 3).

## Deployment via appmod-blueprints

When deployed through the [appmod-blueprints](https://github.com/aws-samples/appmod-blueprints) platform, the `enable_agent_platform` label is set declaratively via `gitops/overlays/environments/<env>/enabled-addons.yaml` in this repo. The platform's fleet-secrets mechanism reads these overlays and applies the label to cluster secrets automatically — no manual labeling required.

## Standalone Deployment

### Prerequisites

- EKS cluster with ArgoCD EKS Capability active
- ArgoCD Capability Role with `AmazonEKSClusterAdminPolicy` on the local cluster
- Local cluster registered in ArgoCD using its EKS ARN
- `kubectl` and `aws` CLI configured
- Crossplane installed with `provider-aws-bedrockagentcore` (for AgentCore resources)
- KubeVela installed (for OAM components)

### Pod Identity (EKS Auto Mode)

LiteLLM needs Bedrock access. Create an IAM role with Pod Identity:

```bash
CLUSTER_NAME="<your-cluster>"
REGION="<your-region>"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

aws iam create-role --role-name ${CLUSTER_NAME}-LiteLLMBedrockRole \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Principal": {"Service": "pods.eks.amazonaws.com"},
      "Action": ["sts:AssumeRole", "sts:TagSession"]
    }]
  }'

aws iam put-role-policy --role-name ${CLUSTER_NAME}-LiteLLMBedrockRole --policy-name BedrockInvoke \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
      "Resource": ["arn:aws:bedrock:*::foundation-model/*",
                    "arn:aws:bedrock:*:'${ACCOUNT_ID}':inference-profile/*"]
    }]
  }'

aws eks create-pod-identity-association \
  --cluster-name $CLUSTER_NAME --region $REGION \
  --namespace litellm --service-account litellm \
  --role-arn arn:aws:iam::${ACCOUNT_ID}:role/${CLUSTER_NAME}-LiteLLMBedrockRole
```

### Deploy

The `appset-chart` generator (sourced from the platform repo, appmod-blueprints — **not** vendored here) generates one ApplicationSet per addon from this repo's `gitops/addons/registry/` files. Deploy it as a multi-source ArgoCD Application.

> **The canonical, maintained copy is [`gitops/bootstrap/agent-platform-app.yaml`](bootstrap/agent-platform-app.yaml)** — a template whose repo coordinates are injected with `envsubst` by `task agentic:bootstrap` (or `scripts/bootstrap.sh`). Prefer that file over the illustrative YAML below: **add new `registry/*.yaml` entries there**, since a hand-maintained duplicate drifts (this block was missing `registry/sandbox.yaml` until it was spotted, which silently omitted every agent-sandbox addon).

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: agent-platform-addons
  namespace: argocd
spec:
  project: default
  sources:
    - ref: values
      repoURL: https://github.com/aws-samples/sample-open-agentic-platform.git
      targetRevision: main
    # Source B — the generator chart, pulled from the PLATFORM repo (reference model).
    - repoURL: https://github.com/aws-samples/appmod-blueprints.git
      path: platform-charts/appset-chart
      targetRevision: feature/agent-platform
      helm:
        releaseName: agent-platform-addons
        ignoreMissingValueFiles: true
        # Registry files (from THIS repo via $values) supply _defaults + addon entries.
        valueFiles:
          - $values/gitops/addons/registry/_defaults.yaml
          - $values/gitops/addons/registry/gateway.yaml
          - $values/gitops/addons/registry/observability.yaml
          - $values/gitops/addons/registry/agentcore.yaml
          - $values/gitops/addons/registry/sandbox.yaml
        valuesObject:
          # Umbrella gate only; per-addon enable_<addon> selectors (useSelectors:true
          # from _defaults) govern placement. Do NOT re-set repoURLGitBasePath here.
          globalSelectors:
            enable_agent_platform: "true"
  destination:
    namespace: argocd
    name: "<REGISTERED_CLUSTER_NAME>"
  syncPolicy:
    automated:
      selfHeal: true
      prune: true
    syncOptions:
      - CreateNamespace=true
      - ServerSideApply=true
```

The hub cluster secret must have `enable_agent_platform: "true"` label for the generated ApplicationSets to match. This label is set declaratively by the fleet-secret chart, which reads `enabledAddons.agent_platform: true` from `gitops/overlays/environments/<env>/enabled-addons.yaml` in this repo. To disable the agentic platform on a specific environment, set `agent_platform: false` in the corresponding overlay.

## How the Agent Platform Works

### Path 1: Agent A2A (direct chat, no auth for internal access)

```
┌──────────┐     ┌───────────────────┐     ┌─────────────┐     ┌─────────┐
│  Client  │────▶│  Agent Pod        │────▶│   LiteLLM   │────▶│ Bedrock │
│  (curl/  │ A2A │  deployed via     │     │  (proxy via │     │  (LLM)  │
│   UI)    │     │  OAM Application  │     │  Pod ID)    │     │         │
└──────────┘     └───────────────────┘     └─────────────┘     └─────────┘
```

1. Client sends JSON-RPC `message/send` to the agent's Service (port 8083)
2. Agent (deployed as a KubeVela `agent` ComponentDefinition + Argo Rollout) handles the A2A protocol
3. Agent calls LiteLLM (OpenAI-compatible) which routes to Bedrock via Pod Identity
4. For tool-using agents, the agent calls MCP servers via the AgentGateway

### Path 2: Authenticated MCP via AgentGateway + KeyCloak

```
┌──────────┐  1.Get Token  ┌──────────────┐
│  Client  │──────────────▶│  KeyCloak    │
│          │◀──────────────│  (platform   │
│          │   JWT Token   │   realm)     │
└────┬─────┘               └──────────────┘
     │
     │ 2. MCP request + JWT
     ▼
┌──────────────────┐  3. Validate JWT   ┌──────────────┐
│  AgentGateway    │───────────────────▶│  KeyCloak    │
│  Proxy (:8080)   │   (JWKS fetch)     │  (JWKS)      │
│  - JWT validation│◀───────────────────│              │
│  - Group authz   │                    └──────────────┘
└────────┬─────────┘
         │ 4. Forward (if in "admin" group)
         ▼
┌─────────────────────────────────────┐
│  MCP Servers (code/browser/memory)  │
└─────────────────────────────────────┘
```

### Testing

```bash
# Chat with an agent (A2A, no auth) — assumes agent deployed in 'default' namespace
kubectl run chat --rm -i --restart=Never --image=curlimages/curl -n default -- \
  -s -X POST http://<agent-name>-stable.default.svc.cluster.local/ \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"message/send","params":{"message":{"role":"user","messageId":"msg-1","parts":[{"type":"text","text":"Hello"}]}}}'

# Get JWT from KeyCloak and test AgentGateway
TOKEN=$(curl -s -X POST "https://<DOMAIN>/keycloak/realms/platform/protocol/openid-connect/token" \
  -d "grant_type=password&client_id=mcp-client&username=user1&password=<PASSWORD>" | jq -r .access_token)

curl -N http://agentgateway-proxy.agentgateway-system.svc.cluster.local:8080/sse \
  -H "Authorization: Bearer $TOKEN"
```

## EKS ArgoCD Capability Notes

- Custom Lua health checks are not supported
- Sync timeout is fixed at 120 seconds
- Git cache refreshes every 3-10 minutes
- Cluster secrets must use EKS ARNs, not `kubernetes.default.svc`
- Pod Identity (not IRSA) for AWS credentials on EKS Auto Mode
- Duplicate cluster secrets with the same ARN are rejected

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| LiteLLM "Unable to locate credentials" | Missing Pod Identity | Create Pod Identity association, restart LiteLLM |
| LiteLLM "model version has reached end of life" | Outdated model IDs | Update to `us.anthropic.*` inference profiles in litellm chart |
| ApplicationSet "map has no entry" | Cluster secret missing annotations | Use `useSelectors: false` with `globalSelectors` |
| langfuse ApplicationSet "map has no entry for key \"hub_cluster_name\"" (blocks all agentic addons — parent `agent-platform-addons` app retries forever and never applies later sync-wave ApplicationSets) | langfuse `secretManagerKey` referenced a non-existent `hub_cluster_name` annotation | Template must use `aws_cluster_name` (the per-cluster annotation set by the fleet-secret chart), not `hub_cluster_name`. Fixed in the langfuse entry of `gitops/addons/registry/observability.yaml`. The `<cluster>/keycloak-clients` Secrets Manager secret must also exist. |
| Gateway API CRDs Job fails | No outbound internet or image pull issue | Verify NAT gateway, check `bitnami/kubectl:latest` availability |
| AgentGateway proxy not starting | Missing Gateway API CRDs or JWKS fetch failure | Verify CRDs installed, check KeyCloak reachability |
| JWT validation fails | Wrong issuer URL | Ensure issuer matches `iss` claim (`https://<domain>/keycloak/realms/platform`) |
| OAM Application stuck "no matches for kind ComponentDefinition" | KubeVela not installed | Install kubevela addon (platform side); verify `oam-agent-components` synced |
| ArgoCD reverts manual changes | selfHeal enabled | Push changes to git instead of patching directly |
