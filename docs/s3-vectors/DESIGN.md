# S3 Vectors RAG for agents

## Goal

Agents answer from documents stored in Amazon S3 Vectors, through an MCP server and
nothing else. A team either deploys an `s3-vectors-mcp` component pointed at its own
index, or lists another team's server in its agent's `mcpServers`.

Origin: showing OAP consuming documents prepared by IBM Docling. Docling converts and
structures the documents; a data pipeline outside OAP chunks, embeds, and writes them
to S3 Vectors. OAP only reads.

## Decisions

| Decision | Choice | Why |
|---|---|---|
| Who loads the index | A pipeline outside OAP (for example Lambda on S3 events) | Ingestion is a data concern; OAP stays a consumer |
| Who creates the bucket and index | The pipeline. OAP assumes they exist | Dimension and metadata keys are fixed at creation and belong to whoever embeds |
| Knowledge Base on top | No. The server calls `QueryVectors` directly | S3 Vectors is the store; a Knowledge Base would add a second ingestion path |
| MCP server | Our own, read-only (`applications/s3-vectors-mcp`) | No AWS Labs server exists (RFC awslabs/mcp#1146 is open); the community one always exposes writes. A generic AWS API server cannot embed the question, so the agent would have to pass ~1,024 floats through its context |
| Embedding | Queries only, Bedrock, default `amazon.titan-embed-text-v2:0` | `QueryVectors` takes a vector; the question must be embedded with the pipeline's model |
| OAM shape | Typed component `s3-vectors-mcp` | Typed bucket/index/model let the component emit an IAM policy scoped to exactly that index |
| Identity | Component emits the policy; `aws-service-identity` with `accessFor: [<own name>]` attaches it | No appmod change. The trait keeps owning the Pod Identity role |
| Sharing | Same cluster, existing grants (`allowedAgents`, `mcpAccess`, `ns/agent`) | Cross-cluster MCP routing does not exist yet |

## Contract with the ingestion pipeline

The pipeline and the server agree on four things. A mismatch in the first two makes
search results meaningless, so the server checks the dimension at startup.

1. **Embedding model.** The same Bedrock model for documents and queries. Set it on
   the component as `embeddingModel`. Supported: Titan Text Embeddings v2
   (256/512/1024), Titan v1 (1536), Cohere Embed v3 English/Multilingual (1024).
2. **Dimension.** The index dimension must be one the model produces. For Titan v2 the
   server requests the index's dimension.
3. **Chunk text** stored in metadata under `textMetadataKey` (default `text`), declared
   non-filterable when the index is created (it is usually larger than the 2 KB
   filterable limit).
4. **Filterable metadata** such as `source` and `page`, returned with every result so
   agents can cite it, and usable in the `search` tool's `filter`.

Vector keys are the pipeline's choice; the server returns them as `key`.

## Design

`s3-vectors-mcp` (`platform/oam/definitions/components/s3-vectors-mcp.cue`) emits:

- The same Rollout, ServiceAccount, Services, `AgentgatewayBackend` and `/mcp/<name>`
  HTTPRoute as `mcp-server`, with fixed probes (`/healthz` liveness, `/readyz`
  readiness) and `sessionRouting: Stateless` (the server keeps no session state).
- An IAM Policy `<appName>-<component>-iam-policy`:
  - `s3vectors:QueryVectors`, `GetVectors`, `GetIndex` on
    `arn:aws:s3vectors:<region>:<account>:bucket/<bucket>/index/<index>` only.
    `GetVectors` is required whenever a query returns metadata or uses a filter
    (QueryVectors API reference).
  - `bedrock:InvokeModel` on that one foundation model, in the cluster's region.
  - `kms:Decrypt` via S3 Vectors, only if `kmsKeyArn` is set. The key policy must
    also allow the role.
- A NetworkPolicy admitting only the gateway's namespace. Enforced only where the
  cluster runs a network policy controller; EKS Auto Mode needs the
  `kube-system/amazon-vpc-cni` ConfigMap with `enable-network-policy-controller: "true"`.

The server exposes two tools, `search(query, top_k, filter)` and `describe_index()`.
It is read-only by construction (no write calls in the code) and by policy.

Not ready, with a reason in `/readyz` and the log, when: the policy is not attached
(names the missing `accessFor` entry), the index does not exist, or the model cannot
match the index dimension. It retries with backoff, so IAM propagation or a pipeline
that creates the index later resolve without a restart.

## Scenario matrix (`scripts/test-s3-vectors-matrix.sh`)

| Row | Scenario | Expected |
|---|---|---|
| F1 | Fixture index | 1024-dim, cosine |
| R1 | Server Rollout | Healthy |
| P1 | IAM policy resources | Exactly the index ARN and the model ARN |
| T1 | `tools/list` through the gateway | `describe_index`, `search` only |
| T2 | `describe_index` | 1024, cosine |
| T3 | `search` "How long does a refund take?" | Refund chunk ranked first |
| T4 | Result shape | Chunk text, `source`, `page` |
| T5 | `filter` on `source` | Only that source |
| T6 | `top_k` above `maxTopK` | Tool error |
| A1 | No token | 401 |
| A2 | ServiceAccount not in `allowedAgents` | 403 |
| A3 | Allowed agent | 200 |
| B1 | Server role queries another index | AccessDenied |
| B2 | Server role writes its own index | AccessDenied |
| N1, N2 | Index the model cannot match | Not Ready; `/readyz` names the dimensions |
| N3, N4 | Trait without `accessFor` | Not Ready; `/readyz` names the fix |
| E1 | User asks the agent | Agent calls the server as its own ServiceAccount |
| E2 | Agent answer | Contains the indexed fact |
| X1 | Direct call bypassing the gateway | Blocked where NetworkPolicy is enforced; reported NOT ENFORCED otherwise |

Portability is checked by rendering with `global.awsRegion=eu-west-1`: every ARN and
`S3VECTORS_REGION` follow it.

## Known limits and follow-ups

- IAM names are account-wide and not cluster-qualified (issue #92). This component
  follows the existing convention and does not make it worse.
- Vector buckets in another account need a bucket policy on the owner's side. Not
  covered.
- Agents reach only servers on their own cluster's gateway, and `/mcp/<name>` has no
  namespace, so one server name per cluster.
- The component copies `mcp-server`'s workload outputs because `vela def render`
  does not resolve local CUE imports. Keep the two in step.
