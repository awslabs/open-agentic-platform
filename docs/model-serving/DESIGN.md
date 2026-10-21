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
| GPU drivers | EKS Auto Mode's own GPU support; no GPU Operator. Phase 0 confirms it on a node. |
| Nodes | EKS Auto Mode (Karpenter) provisions and removes GPU nodes, whatever the engine or autoscaler. |
| Scale to zero | Per model, on by default. KEDA HTTP add-on by default, replaceable per model (see "Autoscaling"). |
| Reachability | A model endpoint accepts traffic only from Bifrost, through the autoscaler's proxy when one is used (row M6). |
| Limits | Rate limits and token quotas per model, no dollar budgets (see "Rate limits and quotas"). |
| Model names | Agents use `<deployment>/<servedName>`; no Bifrost aliases (see layer 5). |

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
- Drivers and device plugin come from EKS Auto Mode; OAP adds no GPU Operator. Phase 0
  confirms on a real `gpu` node that the device plugin advertises `nvidia.com/gpu`. If
  an engine later needs more than that (for example Dynamo's DCGM metrics), the Dynamo
  install guide's "drivers already present" mode applies: GPU Operator with drivers and
  toolkit off.
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
	// Pod autoscaling (see "Autoscaling"). Nodes are always EKS Auto Mode.
	autoscaling: {
		mode:        *"keda-http" | "keda-metrics" | "engine" | "none"
		scaleToZero: *true | bool
		idleMinutes: *30 | int & >=5
		maxReplicas: *1 | int & >=1
		replicas?:   int & >=1 // only with mode "none"
	}
	// Per-model rate limits and token quota, applied in Bifrost (see "Rate limits and
	// quotas"). Omitted means no limit.
	limits?: {...}
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
becomes one provider, named after the component (see layer 5 for why that is unique):

```json
"<component>": {
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

Version note: OAP pins Bifrost `v1.5.0` (`gitops/addons/configs/bifrost/values.yaml`),
and the docs cited here describe current Bifrost. Phase 1 confirms custom providers on
the pinned version, or upgrades Bifrost first.

### Layer 5: agents and model names

No change to the `agent` component. An agent selects a model with
`modelConfig.modelId: "<deployment>/<servedName>"`, for example
`chat-small/qwen3-8b`.

- `<deployment>` is the `llm-model` component name, which is also the Bifrost provider
  name. It is the stable handle: switching engines keeps it.
- `<servedName>` defaults to the component name and can be set to the model's name.
- `llm-model` Applications live in one platform namespace per spoke,
  `global.modelsNamespace` (default `models`, from `config.yaml`). The component rejects
  any other namespace. That makes the component name unique on the spoke, so the
  provider name needs no namespace prefix or hash, and it matches "the platform team
  deploys models".

Bifrost aliases are not used:

- An alias belongs to a provider key, so it is always provider-qualified
  (`<provider>/<alias>`). There is no bare, provider-independent name
  ([Bifrost discussion #4032](https://github.com/maximhq/bifrost/discussions/4032)). An
  alias would not be shorter than the scheme above.
- A user report on Bifrost 1.6.11 says alias requests fail at key selection for the vLLM
  provider ([issue #3278](https://github.com/maximhq/bifrost/issues/3278)). The
  workaround there, routing rules, rewrites the model before virtual-key checks, which
  would bypass per-agent model allowlists.

The way to keep agents stable across model changes is the deployment name: point
`chat-small` at a newer model and agents keep calling `chat-small/...`. Changing
`servedName` with it does change the id, so leave `servedName` at its default when that
matters.

## Rate limits and quotas

No dollar budgets for self-hosted models. Bifrost prices requests from its model
catalog, which has no entries for custom providers (custom providers page), so a dollar
budget would not see them anyway. GPU cost is reported as node-hours, outside Bifrost.

Bifrost rate limits come in two kinds, request limits and token limits, each with a
reset window from `1m` to `1Y`, and can be set on a provider config, a virtual key, a
team or a customer. Inside a virtual key each provider can have its own limits, and a
provider over its limit is dropped from routing while the key's other providers keep
working ([budgets and limits](https://docs.getbifrost.ai/features/governance/budget-and-limits)).

So for a self-hosted model:

| Need | Bifrost setting | Set by |
|---|---|---|
| Protect the GPUs from everyone | rate limit on the model's provider config, short window (`1m`) | `llm-model.limits.requestsPerMinute` / `tokensPerMinute` |
| Quota for the model overall | token limit on the provider config, long window (`1d`, `1M`) | `llm-model.limits.tokenQuota` + `quotaWindow` |
| Quota for one agent on one model | provider-level token limit inside that agent's virtual key | the agent's virtual key, owned by `bifrost-sync` |

`llm-model.limits` becomes:

```cue
limits?: {
	requestsPerMinute?: int & >0
	tokensPerMinute?:   int & >0
	tokenQuota?:        int & >0
	quotaWindow:        *"1d" | "1h" | "1d" | "1w" | "1M"
}
```

Per-agent quotas wait for per-agent virtual keys (`gitops/addons/charts/bifrost/DESIGN.md`).

## Autoscaling

Two separate things scale:

- **Pods**, from zero to `autoscaling.maxReplicas`: the autoscaler chosen per model.
- **Nodes**: EKS Auto Mode (Karpenter). A pending GPU pod gets a node; an empty GPU node
  is consolidated away. This is the same for every engine and autoscaler.

### Default: KEDA HTTP add-on

New addons `keda` and `keda-http-add-on` (neither is in the OAP or base-platform
registries today). `llm-model` emits an `InterceptorRoute` for the model's Service.
What the add-on does, from its docs
([architecture](https://keda.sh/http-add-on/1.0/concepts/architecture/),
[cold start](https://keda.sh/http-add-on/0.16/user-guide/configure-cold-start/),
[timeouts](https://keda.sh/http-add-on/0.16/user-guide/configure-timeouts/)):

- An interceptor proxy sits in the request path, counts requests, and drives KEDA, which
  drives an HPA. Scaling metrics are concurrency or request rate. Concurrency suits LLMs
  better, since one request can run for a minute.
- At zero replicas the interceptor holds the request until the backend is ready, up to a
  readiness timeout, then returns 504, or a placeholder or a fallback Service if set.
  `maxPendingRequests` bounds how many requests it holds.
- Timeouts per route: request (whole lifecycle), response header (after forwarding,
  excluding the cold start), readiness (the cold start).

Path with KEDA: agent → Bifrost → interceptor → model Service. The NetworkPolicy then
admits the interceptor's namespace on the model, and Bifrost on the interceptor (row M6).

What this means for a model:

- A cold start here is GPU node provisioning, a large image pull and the weight
  download, so minutes, not seconds. The readiness timeout, the request timeout, and
  Bifrost's provider timeout must all cover it, and the agent must not give up first.
- Streaming: the request timeout covers the whole response, so it must also cover the
  longest generation. Not verified yet that the interceptor streams server-sent events
  without buffering; row M4b checks it.

### Cold-start behaviour (decision D4)

| Option | What the first caller sees |
|---|---|
| Hold (default proposal) | waits for the cold start, then the answer |
| Fail fast + Bifrost fallback | a 504 after a short readiness timeout; Bifrost retries on a configured fallback model, for example on Bedrock. Bifrost keeps fallbacks available for a 5xx (custom providers page) |
| Placeholder | an immediate 503; the caller retries |

### Replacing the autoscaler

`llm-model.autoscaling.mode`:

| Mode | Use |
|---|---|
| `keda-http` (default) | scale to zero and back on HTTP traffic, any engine |
| `keda-metrics` | KEDA with a Prometheus trigger on engine metrics (for example vLLM's waiting requests); scales 1 to N, not from zero |
| `engine` | the engine's own scaler: Dynamo's planner, llm-d's KEDA-based scale from zero, KServe. The branch renders that engine's resources instead |
| `none` | fixed `replicas` |

`autoscaling.scaleToZero: true` is only valid with `keda-http`, or with `engine` for an engine whose
scaler goes to zero; the component rejects other combinations at apply time.

## Phases

| Phase | Scope | Done when |
|---|---|---|
| 0 | Confirm an Auto Mode `gpu` node advertises `nvidia.com/gpu`; confirm custom providers on the pinned Bifrost | written finding |
| 1 spike | Plain vLLM, one small model as a raw Deployment on spoke-dev; static Bifrost entry (W1); one agent uses it | rows M2-M4, A1 green |
| 2 | `llm-model` with the `vllm` branch; `bifrost-sync` (W2); NetworkPolicy; limits | rows M1-M8, A1-A3 green from a clean install |
| 3 | `keda` + `keda-http-add-on` addons, `autoscaling.mode: keda-http`, scale to zero | rows M4b, M9, M10 green |
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
| M4b | same, through the KEDA interceptor | tokens arrive incrementally, not in one block |
| M5 | delete the Application | provider removed from Bifrost; GPU node released |
| M6 | call the endpoint directly from a namespace other than `bifrost` | blocked |
| M7 | model token secret missing | unhealthy with a clear message; no provider registered |
| M8 | no GPU capacity | stays pending; no provider registered |
| M9 | idle past `autoscaling.idleMinutes`, then one request | scaled to zero, GPU node removed; the request succeeds after the cold start (D4 hold) |
| M10 | `autoscaling.scaleToZero: true` with `autoscaling.mode: none` | rejected at apply |
| M11 | `llm-model` outside `global.modelsNamespace` | rejected at apply |
| L1 | exceed `limits.requestsPerMinute` | Bifrost rejects the excess; other models unaffected |
| L2 | exceed `limits.tokenQuota` in its window | rejected until the window resets |
| A1 | agent with `modelId` set to the model | 200, answer from the self-hosted model |
| A2 | agent whose virtual key has a provider limit for the model, over that limit | rejected for that model; the agent's Bedrock calls unaffected |
| A3 | Bedrock agents on the same spoke | unchanged |
| E1 | same Application with `engine: llm-d` | M1-M6 pass |
| E2 | same Application with `engine: dynamo` / `nim` (opt-in clusters) | M1-M6 pass |
| E3 | change `engine` on a running model | old resources removed, new ones healthy, agents unchanged |
| P1 | spoke on each cluster provider (kind-kro-ack, kind-crossplane) | M1-M4 pass |

## Open questions

- D4 Cold start: hold the request (default proposal), fail fast to a Bifrost fallback
  model, or return a placeholder?
- Bifrost version: does pinned `v1.5.0` support custom providers and provider-config
  rate limits, or does phase 1 start with a Bifrost upgrade?
