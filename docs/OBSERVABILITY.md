# Agent Observability

The platform supports two observability modes for agents. Both work out of the box — no custom image build needed if using the pre-built ECR image.

## Mode 1: Centralized (Default) — Langfuse + AMP/Grafana

```
Agent → OTel Collector (:4318) → Langfuse (traces) + AMP (metrics) → Grafana
```

The agent exports OTLP spans to the local OTel Collector. The collector forwards traces to Langfuse and scrapes Bifrost metrics to AMP.

### How it works

1. OAM `agent` ComponentDefinition injects `OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector.otel.svc.cluster.local:4318`
2. Agent's `main.py` detects the endpoint and calls `StrandsTelemetry().setup_otlp_exporter()`
3. Strands SDK creates spans for agent invocations, tool calls, and LLM requests
4. Bifrost proxy reads the W3C `traceparent` header and creates child spans for model calls
5. OTel Collector merges all spans into one trace tree and exports to Langfuse via OTLP/HTTP with Basic Auth
6. Collector also scrapes Bifrost Prometheus metrics and pushes to AMP via remote write

### Deploy (no config needed — centralized is the default)

```yaml
apiVersion: core.oam.dev/v1beta1
kind: Application
metadata:
  name: my-agent
  namespace: default
spec:
  components:
    - name: my-agent
      type: agent
      properties:
        name: my-agent
        namespace: default
        description: "My agent"
        image: "<account-id>.dkr.ecr.<region>.amazonaws.com/strands-agent:latest"
        systemMessage: "You are helpful."
        modelConfig:
          modelId: claude-sonnet
```

### View traces

Langfuse UI: `https://<ingress_domain>/` (Keycloak SSO)

### View metrics

AMG Grafana: Agent Platform > Bifrost LLM Metrics dashboard

---

## Mode 2: Decentralized — CloudWatch GenAI Console

```
Agent → ADOT auto-instrumentation → CloudWatch (traces + logs + metrics)
```

The agent exports directly to CloudWatch via the AWS OpenTelemetry Distro (ADOT >=0.18.0). No collector or Langfuse needed.

### How it works

1. OAM `agent` ComponentDefinition injects ADOT env vars and overrides the container command to `opentelemetry-instrument python -m app.main`
2. ADOT auto-instruments the FastAPI + Strands application at startup
3. GenAI spans (agent invocations, tool calls, LLM requests) are exported to CloudWatch
4. With ADOT 0.18.0+ (unsplit architecture), spans go directly to the `aws/spans` log group — no separate log group needed
5. Pod Identity on the `default` ServiceAccount provides CW/X-Ray write permissions (provisioned by the `oam-agent-components` chart)

### Deploy

```yaml
apiVersion: core.oam.dev/v1beta1
kind: Application
metadata:
  name: my-agent
  namespace: default
spec:
  components:
    - name: my-agent
      type: agent
      properties:
        name: my-agent
        namespace: default
        description: "My agent"
        image: "<account-id>.dkr.ecr.<region>.amazonaws.com/strands-agent:latest"
        systemMessage: "You are helpful."
        modelConfig:
          modelId: claude-sonnet
        observability:
          mode: decentralized
```

### View traces

CloudWatch Console > Application Signals > GenAI Observability

### Prerequisites (automated by `task install`)

- CloudWatch Transaction Search enabled (one-time per account)
- Pod Identity with `logs:PutLogEvents`, `xray:PutTraceSegments`, `cloudwatch:PutMetricData`

---

## Environment Variables Injected by Mode

| Variable | Centralized | Decentralized |
|----------|-------------|---------------|
| `OTEL_SERVICE_NAME` | ✅ `<agent-name>` | ✅ `<agent-name>` |
| `OTEL_TRACES_EXPORTER` | ✅ `otlp` | ✅ `otlp` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | ✅ collector :4318 | — |
| `OTEL_PYTHON_DISTRO` | — | ✅ `aws_distro` |
| `OTEL_PYTHON_CONFIGURATOR` | — | ✅ `aws_configurator` |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | — | ✅ `http/protobuf` |
| `OTEL_RESOURCE_ATTRIBUTES` | — | ✅ `service.name=<name>` |
| `AGENT_OBSERVABILITY_ENABLED` | — | ✅ `true` |
| Container command | `python -m app.main` | `opentelemetry-instrument python -m app.main` |

---

## Agent Image

Both modes use the same image. The image includes all dependencies for both paths.

### Dependencies (pyproject.toml)

```toml
dependencies = [
    "fastapi~=0.115.0",
    "uvicorn[standard]>=0.34.2",
    "pydantic~=2.0",
    "strands-agents[a2a,openai,otel]~=1.0",
    "bedrock-agentcore",
    "aws-opentelemetry-distro>=0.18.0",
]
```

- `strands-agents[otel]` — brings OpenTelemetry SDK + OTLP HTTP exporter (centralized)
- `aws-opentelemetry-distro>=0.18.0` — ADOT with unsplit architecture (decentralized)

### Dockerfile

```dockerfile
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder
WORKDIR /app
COPY pyproject.toml ./
RUN uv pip install --system --no-cache --prerelease=allow .

FROM python:3.13-slim-bookworm
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY app/ ./app/
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser
EXPOSE 8083
CMD ["python", "-m", "app.main"]
```

### Build and Push

```bash
cd applications/strands-agent-base
IMAGE_NAME=strands-agent IMAGE_TAG=latest AWS_REGION=us-east-1 ./build.sh push
```

Output: `<account-id>.dkr.ecr.us-east-1.amazonaws.com/strands-agent:latest`

---

## Testing Agents

### Deploy a centralized agent and invoke

```bash
# Deploy
kubectl apply -f platform/oam/examples/example-agent-centralized-observability.yaml

# Wait for pod
kubectl get pods -l app.kubernetes.io/name=my-agent -w

# Invoke
kubectl run test --image=curlimages/curl --rm -it --restart=Never -- \
  curl -s -X POST http://my-agent-stable.default.svc.cluster.local:8083/ \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"message/send","id":"1","params":{"message":{"role":"user","messageId":"t1","parts":[{"type":"text","text":"What is S3?"}]}}}'

# Verify trace in Langfuse
curl -s "https://<domain>/api/public/traces?limit=1" \
  -H "Authorization: Basic $(echo -n pk-lf-otel-platform:sk-lf-otel-platform-2026 | base64)"
```

### Deploy a decentralized agent and invoke

```bash
# Deploy
kubectl apply -f platform/oam/examples/example-agent-decentralized-observability.yaml

# Wait for pod
kubectl get pods -l app.kubernetes.io/name=my-cw-agent -w

# Invoke
kubectl run test --image=curlimages/curl --rm -it --restart=Never -- \
  curl -s -X POST http://my-cw-agent-stable.default.svc.cluster.local:8083/ \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"message/send","id":"1","params":{"message":{"role":"user","messageId":"t1","parts":[{"type":"text","text":"What is Lambda?"}]}}}'

# Verify trace in CloudWatch
# Console > CloudWatch > Application Signals > GenAI Observability
```

---

## Platform Infrastructure

### Centralized (provisioned by task install)

| Component | Location | Purpose |
|-----------|----------|---------|
| OTel Collector | `otel` namespace (all clusters) | OTLP receiver + Langfuse export + AMP remote write |
| Langfuse v3 | `langfuse` namespace (hub only) | Trace UI + OTLP endpoint |
| AMP Workspace | AWS (Crossplane-provisioned) | Prometheus metrics storage |
| AMG Workspace | AWS (Crossplane-provisioned) | Grafana dashboards |
| Bifrost | `bifrost` namespace (all clusters) | LLM proxy with OTEL plugin + metrics |

### Decentralized (provisioned by task install)

| Component | Location | Purpose |
|-----------|----------|---------|
| Pod Identity | `vela-system` namespace (Crossplane) | IAM role with CW/X-Ray permissions |
| Transaction Search | AWS account-level | One-time CW config (enabled in Taskfile) |

---

## Troubleshooting

| Problem | Check |
|---------|-------|
| No traces in Langfuse | Agent logs for `StrandsTelemetry` init. Collector logs for export errors. ExternalSecret `langfuse-otel-auth` status. |
| No traces in CloudWatch | Pod Identity association exists (`kubectl get podidentityassociation -n vela-system`). Agent logs for ADOT init. Transaction Search enabled. |
| Cost shows $0 in Langfuse | Seed CronJob logs (`kubectl logs -n langfuse -l app=langfuse-seed`). Check 401 auth errors. |
| Agent crashes on startup | Check if image has ADOT packages. Verify port 4318 (not 4317) for centralized. |
| Metrics not in Grafana | The collector takes the AMP endpoint from the `amp_endpoint_url` cluster-secret annotation, else from the `<resource_prefix>/platform/amp` Secrets Manager secret (`otel-collector-amp` ExternalSecret in `otel`). Check that ExternalSecret is synced, then the collector logs for AMP errors. |
