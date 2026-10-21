# Self-hosted model serving behind Bifrost

Status: draft for review. Nothing here is built yet.

## Goal

A platform team can:

1. enable an inference engine on a spoke (open source by default, NVIDIA engines opt-in);
2. deploy a model with one OAM component, `llm-model`;
3. have the model registered in that spoke's Bifrost automatically;

and an agent uses it by changing `modelConfig.modelId`, with no other change.

Changing the engine must not change any agent, and changes at most one field of the
model's Application.

## Decisions

| | Decision |
|---|---|
| Placement | Models run on each spoke that enables them, served by that spoke's Bifrost. No cross-cluster model sharing. |
| Ownership | Everything in OAP: engine addons, `llm-model`, the Bifrost wiring, and any GPU driver addon. |
| Default | Nothing is provisioned until a platform team applies an `llm-model`. |
| Engines | Open-source engines are the base path. NVIDIA engines (Dynamo, NIM) are opt-in for customers with an NVIDIA relationship. |
| Scale to zero | Configurable per model, on by default (row M9). |
| Reachability | A model endpoint accepts traffic only from Bifrost (row M6). |
| Limits | Per model, through Bifrost provider-level limits (see "Limits and budgets"). |

## What exists today

- Agents call Bifrost at `modelConfig.llmGatewayUrl` (default
  `http://bifrost.bifrost.svc.cluster.local:8080/v1`) with `modelId` default
  `claude-sonnet` (`platform/oam/definitions/components/agent.cue:454-457`).
- Bifrost runs on every cluster that enables it (`bifrost: true` in the control-plane,
  dev and prod `enabled-addons.yaml`). Providers are static Helm values; today only
  Bedrock (`gitops/addons/configs/bifrost/values.yaml`). The config store is Postgres.
- The base platform ships an EKS Auto Mode `gpu` NodePool
  (`.platform/gitops/addons/registry/platform.yaml`, `gpu.enabled`).
- AgentGateway runs on every spoke.
- No inference engine, model abstraction or GPU driver addon exists in OAP yet.

## Layers

```
 agent (OAM agent)        modelConfig.modelId: "<provider>/<servedName>"
        |
 Bifrost (per spoke)      one custom OpenAI-type provider per model        (layer 4)
        |
 model endpoint           OpenAI-compatible base URL, only Bifrost may call (layer 3 contract)
        |
 engine                   vllm | llm-d  (OSS)    dynamo | nim  (NVIDIA, opt-in)   (layer 2)
        |
 GPU substrate            gpu NodePool, drivers, model sources                      (layer 1)
```

Each layer depends only on the contract of the layer below it, which is what makes the
engine replaceable.

### Layer 1: GPU substrate

- Nodes come from the existing `gpu` NodePool.
- Drivers and device plugin: open question Q1. EKS Auto Mode GPU nodes may already
  provide them. If they do, nothing is added. If not, OAP adds a `nvidia-gpu-operator`
  addon. The Dynamo install guide says to install the GPU Operator with drivers and
  toolkit off when the nodes already have drivers, so the two are never both fully on.
- Model sources: Hugging Face for the OSS tier, NGC for NIM. Tokens (`HF_TOKEN`,
  `NGC_API_KEY`) come through External Secrets and never appear in an Application.
- Weights are downloaded per pod in phases 1 to 3, and come from a shared cache (S3 or
  FSx) in phase 4.

### Layer 2: engines

| Tier | Engine | Addon installed | What `llm-model` renders |
|---|---|---|---|
| OSS (default) | vLLM | none | Deployment + Service running the vLLM OpenAI server |
| OSS | llm-d | `llm-d` (Gateway API Inference Extension CRDs, endpoint picker) | vLLM pods + `InferencePool`, routed by AgentGateway |
| NVIDIA opt-in | Dynamo | `dynamo` (`dynamo-crds` + `dynamo-platform`) | `DynamoGraphDeployment` |
| NVIDIA opt-in | NIM | `nim-operator` | `NIMService` (+ `NIMCache`) |
| later | KServe | `kserve` | `LLMInferenceService` |

The plain vLLM engine needs no addon, which is why "nothing provisioned by default"
still gives a working path: the first `llm-model` applied is what creates anything.

Dynamo itself is Apache-2.0 licensed (`ai-dynamo/dynamo` LICENSE). It sits in the
opt-in tier because its value comes with NVIDIA's stack, not because of its license.
NIM images are NVIDIA-licensed. My understanding, not verified here, is that NIM in
production needs an NVIDIA AI Enterprise license.

Dynamo's optional parts (Grove/KAI for multi-node, RDMA for disaggregated serving,
Prometheus for the planner) are separate flags on the `dynamo` addon, off by default.

### Layer 3: the `llm-model` component

One ComponentDefinition, with its CUE source in
`platform/oam/definitions/components/llm-model.cue`, generated into the
`oam-agent-components` chart like the others.

Parameters (a proposal):

```cue
parameter: {
	// Hugging Face model id, or the NIM image for engine "nim".
	model: string
	// Name agents use in modelConfig.modelId. Defaults to the component name.
	servedName: *context.name | string
	// Engine. Defaults to the cluster's platform setting, so the same Application is
	// portable (oam-authoring.md section 1).
	engine: *"{{ .Values.global.inferenceEngine }}" | "vllm" | "llm-d" | "dynamo" | "nim"
	// Size, not instance types: the platform maps it to GPU count and the gpu NodePool.
	gpus: *1 | int & >=1
	// Scale to zero when idle. On by default.
	scaleToZero: *true | bool
	idleMinutes: *30 | int & >=5
	maxReplicas: *1 | int & >=1
	// Per-model limits, applied in Bifrost (layer 4). Omitted means no limit.
	limits?: {
		requestsPerMinute?: int & >0
		tokensPerMinute?:   int & >0
	}
	// Engine-specific settings, passed through unchanged.
	engineConfig?: {...}
}
```

The template has one CUE branch per engine. Every branch must meet the same
**endpoint contract**:

| Item | Requirement |
|---|---|
| API | OpenAI-compatible `/v1/chat/completions` (streaming) and `/v1/models` |
| Output | KubeVela output `endpoint`: the base URL Bifrost uses, without `/v1` |
| Served name | `/v1/models` lists `parameter.servedName` |
| Health | The component is healthy only once the engine reports the model ready |
| Network | Ingress only from the `bifrost` namespace, through a NetworkPolicy emitted by the component |
| Identity | Pods follow the existing component shape: a ServiceAccount named `context.name` |

Examples of `endpoint`:
- vLLM: a Service named after the component.
- Dynamo: a Service the branch adds in front of Dynamo's frontend, so the URL doesn't
  depend on Dynamo's own naming.
- llm-d: the AgentGateway route to the `InferencePool`.

Adding an engine means adding one branch that meets this contract. Agents, the Bifrost
wiring and existing Applications don't change.

`engine` defaults to `global.inferenceEngine`. It comes from a new `config.yaml` field
`inference.defaultEngine` (default `vllm`), wired through the cluster secret annotation
like `aws_region`. That makes it an explicit customer input with a default
(`platform-portability.md` section 3).

Rejected alternative: one ComponentDefinition per engine. Switching engines would then
mean editing every Application's `type`, and nothing would make the engines share one
parameter schema.

### Layer 4: wiring into Bifrost

Bifrost supports custom providers with `custom_provider_config.base_provider_type:
openai` and `network_config.base_url`. Clients address them as `<provider>/<model>`
([custom providers](https://docs.getbifrost.ai/providers/custom-providers)). Each model
becomes one provider, named `<namespace>-<component>`:

```json
"<namespace>-<component>": {
  "keys": [{"name": "default", "value": "unused", "models": ["<servedName>"], "weight": 1}],
  "network_config": {"base_url": "<endpoint output>"},
  "custom_provider_config": {"base_provider_type": "openai"}
}
```

How the entry gets there:

| Option | How | Pros | Cons |
|---|---|---|---|
| W1 static | the platform team adds the entry to `gitops/addons/configs/bifrost/values.yaml` | no new code | not self-service; Bifrost restarts on every change; the model and its entry can drift |
| **W2 reconciler (chosen from phase 2)** | `llm-model` emits a `BifrostProvider` resource; a `bifrost-sync` controller upserts it through Bifrost's management API and removes it on delete (finalizer) | declarative; no Bifrost restart; deletion handled; the same controller can own per-agent virtual keys (`gitops/addons/charts/bifrost/DESIGN.md`) | a controller to build and run; Bifrost admin auth must be on |
| W3 workflow step | a KubeVela workflow step calls the API | no controller | removes nothing on delete; retries are ad hoc |

Bifrost's DB-backed mode keeps entities created through the API across restarts, and
only lets `config.json` overwrite entities defined in the file
([setting up](https://docs.getbifrost.ai/quickstart/gateway/setting-up)). So W2 entries
and the static Bedrock entry coexist.

Phase 1 uses W1 to prove the path. W2 replaces it in phase 2.

### Layer 5: agents

No change to the `agent` component. An agent selects a model with
`modelConfig.modelId: "<namespace>-<component>/<servedName>"`. A short alias would be
nicer, but whether Bifrost aliases work for custom providers is unverified (the current
Bedrock entry uses `aliases`).

## Limits and budgets

Bifrost's governance works through virtual keys, with optional teams and customers above
them. Budgets and rate limits are set at those levels. Inside a virtual key, each
provider can have its own budget and rate limit, and a provider that exceeds its own
limit is dropped from routing while the key's other providers keep working
([budgets and limits](https://docs.getbifrost.ai/features/governance/budget-and-limits)).
Rate limits can also be set on the provider config itself.

Since every self-hosted model is its own provider, "per-model limits" means:

- **Per model, for everyone:** `llm-model.limits` becomes a rate limit (requests and
  tokens per minute) on that model's provider config. It protects the GPUs whoever
  calls.
- **Per model, per agent:** the agent's virtual key carries a provider-level limit for
  that model. This comes with the per-agent virtual keys already planned in
  `bifrost/DESIGN.md`, which `bifrost-sync` would own.

Dollar budgets are the weak spot. Bifrost computes cost from its model catalog, and the
catalog has no entries for custom providers (custom providers page), so a self-hosted
model's requests probably cost 0 against a dollar budget. I haven't verified whether
custom pricing can be set. Until then, limits on self-hosted models are token and
request rate limits, and dollar budgets keep applying to Bedrock only. GPU cost is
better tracked as node-hours, outside Bifrost.

## Scale to zero

The default is on, idle for 30 minutes. The mechanism is not chosen yet (Q2):

- KEDA HTTP add-on in front of the model Service: scales 0 to 1 on the first request;
  generic for every engine.
- The engine's own autoscaler (Dynamo planner, KServe). Engine-specific, and not every
  engine scales to zero.

Either way, a cold start includes GPU node provisioning and the weight download,
probably minutes. Bifrost's request timeout and the agent's retry need to cover that,
or the first call fails. That is row M9.

## Phases

| Phase | Scope | Done when |
|---|---|---|
| 0 | Check GPU drivers on an Auto Mode `gpu` node; decide Q1 | written finding |
| 1 spike | Plain vLLM, one small model as a raw Deployment on spoke-dev; static Bifrost entry (W1); one agent uses it | rows M2-M4, A1 green |
| 2 | `llm-model` with the `vllm` branch; `bifrost-sync` (W2); NetworkPolicy; limits | rows M1-M8, A1-A3 green from a clean install |
| 3 | Scale to zero | row M9 green |
| 4 | `llm-d` branch | rows E1, E3 green |
| 5 | NVIDIA tier: `dynamo` and `nim` branches, Nemotron through NIM | row E2 green per engine |
| later | Shared weight cache; Grove/KAI for large models; disaggregated serving on EFA/RDMA | per feature |

## Scenario matrix (becomes `scripts/test-model-matrix.sh`)

| Row | Scenario | Expected |
|---|---|---|
| M1 | apply `llm-model` with defaults | healthy only after the engine reports the model ready |
| M2 | `GET /v1/models` on the endpoint | lists `servedName` |
| M3 | Bifrost provider after M1 | `<ns>-<component>` exists with the endpoint as base URL |
| M4 | streaming chat completion through Bifrost | 200, tokens streamed |
| M5 | delete the Application | provider removed from Bifrost; GPU node released |
| M6 | call the endpoint directly from a namespace other than `bifrost` | blocked |
| M7 | model token secret missing | unhealthy with a clear message; no provider registered |
| M8 | no GPU capacity | stays pending; no provider registered |
| M9 | idle past `idleMinutes`, then one request | scaled to zero; the request succeeds after a cold start |
| L1 | exceed `limits.requestsPerMinute` | Bifrost rejects the excess; other models unaffected |
| A1 | agent with `modelId` set to the model | 200, answer from the self-hosted model |
| A2 | agent whose virtual key has a provider limit for the model, over that limit | rejected for that model; the agent's Bedrock calls unaffected |
| A3 | Bedrock agents on the same spoke | unchanged |
| E1 | same Application with `engine: llm-d` | M1-M6 pass |
| E2 | same Application with `engine: dynamo` / `nim` (opt-in clusters) | M1-M6 pass |
| E3 | change `engine` on a running model | old resources removed, new ones healthy, agents unchanged |
| P1 | spoke on each cluster provider (kind-kro-ack, kind-crossplane) | M1-M4 pass |

## Open questions

- Q1 Drivers: is Auto Mode's GPU support enough, or does OAP add `nvidia-gpu-operator`
  with drivers off? Phase 0 answers this.
- Q2 Scale to zero: the KEDA HTTP add-on (generic) or the engine's own autoscaler?
- Q3 Model aliases in Bifrost for custom providers, so agents can use a short name.
- Q4 Dollar budgets for self-hosted models: needed, or are rate limits plus node-hour
  reporting enough?
