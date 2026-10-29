# Continuous Agent Evaluation: LLM-as-a-Judge on Agent Observability Traces

| | |
|---|---|
| **Status** | Accepted; Phase 1 implemented |
| **Implementation** | Reconciler `applications/agent-evaluation/`, addon `gitops/addons/charts/agent-evaluation/` |
| **Author** | @elamaran11 |
| **Reviewers** | TBD |
| **Target** | OAP agent platform: `agent` component, new `agent-evaluation` addon |
| **Date** | 2026-10-22 |
| **Revision** | 5 (Phase 1 implemented; rule ownership moved from a cluster-name prefix to an exact owner token, which closes the `dev` / `dev-2` prefix overlap) |
| **Issue** | #79 |

## Scope

**In scope**

- Online evaluation of deployed agents from the traces they already emit
- A declarative `evaluation` block on the `agent` component, so evaluation is
  configured in the agent's OAM Application and not wired by hand in a UI
- A judge model chosen by the platform, overridable per agent
- Per-agent quality reports over a rolling window, for example *"91% of 1,000
  sampled interactions passed `goal-completion`"*, on a Grafana dashboard
  shipped with the addon

**Not in scope**

- Multi-agent and run-level scoring (#80)
- Offline dataset experiments that gate a promotion (Phase 3 below)
- Human annotation queues

---

## 1. Summary

An application developer turns on evaluation by adding one block to an agent:

File: `platform/oam/examples/example-agent-evaluation.yaml` (proposed)

```yaml
apiVersion: core.oam.dev/v1beta1
kind: Application
metadata:
  name: support-assistant
spec:
  components:
    - name: support-assistant
      type: agent
      properties:
        systemMessage: "You are a support assistant."
        modelConfig:
          modelId: claude-sonnet
        evaluation:
          enabled: true
          # Optional. Defaults to the platform judge; see D3.
          judge:
            modelId: judge-default
          strictness: medium      # low | medium | high: how high a score must be to pass
          coverage: medium        # low | medium | high: how many traces are scored
          evaluators:
            - goal-completion
            - helpfulness
            - name: tool-use
              strictness: high    # optional per-evaluator override
```

From that block, the platform configures **Langfuse online LLM-as-a-judge
evaluators** that score a sample of this agent's live traces as they arrive, and
store each score on the trace that produced it. The judge model is reached
through **Bifrost**, so judge calls share the platform's model access, budgets and
cost tracking. No new credentials or UI steps are needed.

The recommendation is **not** a standing RAGAS service. D1 explains why, and keeps
a place for RAGAS in offline evaluation.

---

## 2. Background

### 2.1 How agents are observed today

Each agent already emits OpenTelemetry traces. The `observability.mode` parameter
on the `agent` component selects where they go:

| Mode | Path | Where |
|---|---|---|
| `centralized` (default) | Agent → OTel Collector → hub Langfuse (the spoke collector exports to the single fleet Langfuse). `agent.cue` also emits `LANGFUSE_*` for a direct path in every mode, but they default to empty, so the collector is the path in use | `platform/oam/definitions/components/agent.cue` (`OTEL_EXPORTER_OTLP_ENDPOINT`), `gitops/addons/registry/observability.yaml` |
| `decentralized` | Agent → ADOT → CloudWatch GenAI Observability | same file (`OTEL_PYTHON_DISTRO=aws_distro`) |

Every trace carries the agent's name as a Langfuse tag
(`applications/strands-agent-base/app/agent.py`, `_trace_attributes`:
`langfuse.trace.tags = [source:<chat|rca>, <AGENT_NAME>]`), plus `session.id` and
`user.id`. That tag is the bare component name, with no cluster or namespace in it,
and every spoke sends to the same hub Langfuse project. So the same agent deployed
on `dev` and `prod` produces identical tags. D4 adds a cluster-scoped tag for that
reason.

### 2.2 What is missing

Nothing reads those traces back and judges them. The README capability table lists
**Agent Evaluation** as `(planned)`.

### 2.3 How evaluation is done in practice

There are three common patterns:

| Pattern | What it is | Typical tools |
|---|---|---|
| **Online evaluation** | An LLM judge scores a sample of live production traces as they arrive, and scores land next to the trace | Langfuse evaluators, AgentCore Evaluations online mode, LangSmith online evaluators, Arize Phoenix |
| **Offline evaluation** | A fixed dataset is replayed against a new agent version before release, and scores are compared | Langfuse datasets and experiments, RAGAS, DeepEval, AgentCore batch evaluation |
| **Human review** | A person labels a sample to calibrate the judge | Langfuse annotation queues |

The issue's use case (an agent accumulates real interactions over time and gets
a quality report) is **online evaluation**. The observability store runs the
evaluation itself, because that is where the traces already are, and the scores
are written back onto the traces. That is the model both Langfuse and AgentCore
Evaluations ship.

### 2.4 The common pattern on Kubernetes

Teams running agents on EKS with a self-hosted Langfuse usually set up online
evaluation like this:

- **Langfuse LLM-as-a-judge**, with the judge reached through an
  **OpenAI-compatible gateway**: an LLM connection whose base URL is the
  in-cluster model gateway (LiteLLM or similar).
- **A few custom evaluators**, typically accuracy, helpfulness and safety, plus
  domain ones. Each is a prompt over `{{input}}` and `{{output}}` that returns a
  **numeric score from 0 to 1**, with a reasoning prompt and an output prompt.
- **A target and a filter on every evaluator**, matching on trace name or tag,
  so matching traces are scored automatically as they arrive.
- **Dashboards** built on the scores: average by dimension, distribution, a
  list of low scores, and quality broken down by model.

These steps are normally done by hand in the Langfuse UI. This design makes them
declarative: reconcilers create the LLM connection, the evaluators, and the
targets and filters through the Langfuse API (D4), from the agent's own OAM
Application. The project-wide objects have one owner on the hub, and each cluster
owns only its own rules (D4). OAP's gateway is Bifrost (D3).

Two details of that setup shape this design:

- **Langfuse blocks cluster-internal LLM endpoints by default** (SSRF
  protection). Hand-built setups fix it with
  `kubectl set env ... LANGFUSE_LLM_CONNECTION_WHITELISTED_HOST`. OAP has to make
  the same change in git instead (`no-manual-fixes.md`); see D3.
- **Older setups use trace-level evaluation.** Langfuse now recommends
  observation-level evaluation and has deprecated the trace-level kind, so this
  design targets observations (D4).

---

## 3. Decisions

### D1. Evaluation engine: Langfuse online evaluators, not a RAGAS service

| | A. Langfuse online evaluators (**chosen**) | B. RAGAS service reading Langfuse traces | C. AgentCore Evaluations |
|---|---|---|---|
| Runs where traces already are | Yes, in the Langfuse worker | No, polls Langfuse, scores, writes back | Yes, for CloudWatch traces |
| Online, sampled, filtered | Built in: sampling, observation and trace filters | Must be built: polling, cursoring, sampling, retries | Built in: sampling, agent selection, session timeout |
| Agent metrics without ground truth | Rubric-based LLM judge, any criterion | Mostly RAG metrics. Agent metrics (`tool_call_accuracy`, `tool_call_f1`, goal accuracy with a reference) need ground truth that live traffic does not have | 13 built-in evaluators, plus custom ones |
| Configurable without a UI | Yes, public API: `/api/public/unstable/evaluators`, `/evaluation-rules` at the pinned `3.225.11`; stable endpoints on Langfuse `main` | Yes, our code | Yes: API, CLI and `AWS::BedrockAgentCore::OnlineEvaluationConfig` |
| New moving parts | One small reconciler (D4) | A service, a scheduler, a state store, a score writer | Traces must be in CloudWatch |
| Upstream activity | Several Langfuse releases a week (`v4.64.0`–`v4.68.0` between 2026-10-19 and 2026-10-22) | RAGAS: last release `v0.4.3` on 2026-01-13, last commit 2026-02-24 | AWS managed, GA |

**Why A.**

- **The engine already exists in the platform.** Langfuse supports online
  evaluation with sampling and filters, can judge an observation's input, output,
  metadata or tool calls, and writes scores to the trace. A RAGAS service would
  re-implement the polling, sampling and score writing that Langfuse already does.
- **RAGAS is a library, not a service, and is aimed mostly at RAG.** Its agent
  metrics compare against a reference. Live agent traffic has no reference
  answer, so the reference-free LLM-judge pattern is the one that fits online
  evaluation.
- **It works for the default observability mode** (`centralized`), so every agent
  can turn it on today.

**Why not RAGAS as the engine (B).**

- **It is a library, not a service.** RAGAS computes metrics on samples you hand
  it. It has no trace ingestion, sampling, scheduling or score storage, so
  "listens to Langfuse traces" means building and operating a service around
  it: a poller with a cursor, a sampler, retries, a state store and a score
  writer. Langfuse already runs all of that in its worker.
- **Its agent metrics need ground truth that live traffic does not have.**
  `ToolCallAccuracy` and `ToolCallF1` compare against `reference_tool_calls`, and
  `TopicAdherenceScore` against `reference_topics`. Only
  `AgentGoalAccuracyWithoutReference` works without a reference. The rest of the
  metrics directory is mostly RAG-specific: context precision and recall,
  faithfulness, noise sensitivity.
- **Upstream has slowed.** The last release was `v0.4.3` on 2026-01-13, and the
  last commit on `main` was on 2026-02-24. Platform code built on it would depend
  on a project that has not shipped in nine months.
- **Scores would live apart from the traces.** Writing them back to Langfuse is
  more code we own. The native evaluators write the score on the trace they
  judged.

**Why not AgentCore Evaluations as the engine (C).**

- **It reads traces from CloudWatch, and OAP's default traces go to Langfuse.**
  Its prerequisites need "AgentCore Observability, including Transaction Search".
  In OAP that is `observability.mode: decentralized` (ADOT to CloudWatch).
  The default `centralized` agents send to Langfuse and would have to change
  observability mode, or export twice, before they could be evaluated.
- **Built-in evaluators bypass Bifrost.** For built-in and third-party evaluators
  "the service selects and runs the judge model on its own capacity". So the
  platform cannot choose the judge or route its calls through Bifrost, and judge
  cost is outside Bifrost's budgets and tracking. Custom evaluators let us choose
  the judge, but it is invoked through the evaluation's IAM execution role,
  still not through Bifrost.
- **Results live in CloudWatch**: a log group per configuration plus metrics.
  Teams that debug in Langfuse would read scores in a second tool.
- **Configuring it declaratively needs a provider upgrade.** The Crossplane
  `OnlineEvaluationConfig` managed resource first ships in
  `provider-upjet-aws` `v2.7.0` (2026-08-10). `crossplane-agentcore` pins
  `v2.5.3` (`gitops/addons/charts/crossplane-agentcore/values.yaml`).

**Where the others fit.**

- **C is the backend for `observability.mode: decentralized`.** AgentCore
  Evaluations does not require AgentCore Runtime: its prerequisites accept "an
  agent built with a supported framework configured with AgentCore Observability,
  including Transaction Search". Strands Agents is a supported framework. For
  those agents the traces are already in CloudWatch, so every objection above
  except the Bifrost one goes away. The same `evaluation` block then maps to an
  `OnlineEvaluationConfig` (Phase 2, after the provider upgrade).
- **RAGAS fits offline evaluation.** Reference-based metrics are useful when a
  dataset supplies the references. A Job runs a dataset through the agent before
  promotion and scores it (Phase 3).

### D2. The abstraction: an `evaluation` block on `agent`, backed by a platform catalog

The application developer chooses **which** evaluators run, **how strict** they
are, **how many** traces are scored, and optionally **which judge**. The platform
engineer owns **what each evaluator means**: its prompt, scoring scale and
variable mapping, and what each level resolves to, in the addon's catalog. That
keeps judge prompts reviewed and versioned in one place, the same way
`mcpServers` names a server without restating how to reach it. A planned
follow-up lets a team ship a domain evaluator as its own component instead of as
a PR to that catalog, without changing this block (D4, "Planned follow-up").

Two levels let a developer tune evaluation without knowing the numbers:

| Level | Means | Resolves to | Defined in |
|---|---|---|---|
| `strictness: low \| medium \| high` | How high a score must be to count as passed | A `passThreshold` per evaluator and level, for example `safety` 0.8 / 0.9 / 0.97 | Catalog (D4) |
| `coverage: low \| medium \| high` | What fraction of traces is scored, which sets judge cost | A platform sampling rate per level, for example 0.01 / 0.1 / 0.5 | Addon values (D4) |

```mermaid
flowchart LR
    classDef dev fill:#dbeafe,stroke:#2563eb,color:#1e3a8a
    classDef plat fill:#fef3c7,stroke:#d97706,color:#78350f
    classDef out fill:#dcfce7,stroke:#16a34a,color:#14532d
    S["strictness: high<br/>(agent, or per evaluator)"]:::dev --> T["catalog passThreshold<br/>safety.high = 0.97"]:::plat
    C["coverage: low"]:::dev --> P["addon values<br/>coverage.low = 0.01"]:::plat
    X["sampling: 0.25<br/>(optional)"]:::dev -. overrides .-> P
    T --> RPT["Report: pass if score ≥ 0.97<br/>(D5, no Langfuse rule change)"]:::out
    P --> RULE["Langfuse rule sampling<br/>(D4, part of the rule group)"]:::out
```

`strictness` is set for the whole agent and can be overridden per evaluator.
`sampling` stays as an exact-number escape hatch that overrides `coverage`.
Prompts, score type and target are **not** levels. Changing them per agent would
need one Langfuse evaluator per agent, and they describe the trace format, not
the agent.

File: `platform/oam/definitions/components/agent.cue` (proposed `parameter` addition)

```cue
// +usage=Online evaluation of this agent's live traces by an LLM judge.
// Requires the agent-evaluation addon on the cluster.
evaluation?: {
	// +usage=Turn evaluation on for this agent.
	enabled: *false | bool
	// +usage=Judge model, as a Bifrost model alias from the platform's judge.aliases
	// list. Defaults to the platform judge. An alias not on the list is reported on
	// the agent and its evaluators are skipped.
	judge?: modelId: string
	// +usage=How high a score must be to pass: low, medium or high. The platform
	// catalog defines the threshold for each evaluator at each level.
	strictness: *"medium" | "low" | "high"
	// +usage=How many traces are scored: low, medium or high. The platform defines
	// the sampling rate for each level. Each scored trace costs one judge call per evaluator.
	coverage: *"medium" | "low" | "high"
	// +usage=Exact fraction of traces scored, 0-1. Overrides coverage.
	sampling?: number & >0 & <=1
	// +usage=Evaluators from the platform catalog: a name, or {name, strictness}
	// to override strictness for one evaluator. Names are checked by the
	// reconciler, which owns the catalog: CUE renders without cluster reads, so it
	// cannot see catalog entries teams add.
	// A bare name is a platform catalog entry. The `<namespace>/<name>` form is
	// reserved for team-defined evaluators (D4, "Planned follow-up") and is not
	// accepted in Phase 1, so widening #EvaluatorName is part of that follow-up.
	evaluators: *["goal-completion", "helpfulness"] | [...(#EvaluatorName | {
		name:        #EvaluatorName
		strictness?: "low" | "medium" | "high"
	})]
}

// Single hyphens only: "--" separates an evaluator from a judge alias in the
// Langfuse object name (D4), so a catalog name must not contain it.
#EvaluatorName: =~"^[a-z0-9]+(-[a-z0-9]+)*$"
```

There is no region, account, endpoint or key in the block (`oam-authoring.md` §1).
The component emits a single declaration object, and the reconciler in D4 owns
everything else:

File: `platform/oam/definitions/components/agent.cue` (proposed output, rendered when `evaluation.enabled`)

```cue
if parameter.evaluation != _|_ && parameter.evaluation.enabled {
	outputs: evaluationSpec: {
		apiVersion: "v1"
		kind:       "ConfigMap"
		metadata: {
			name:      context.name + "-evaluation"
			namespace: context.namespace
			labels: "evaluation.oap.io/agent": context.name
		}
		data: {
			agent:      context.name
			backend:    parameter.observability.mode // centralized -> Langfuse, decentralized -> AgentCore
			// The agent's model alias, so reports can compare quality by model (D5).
			model:      parameter.modelConfig.modelId
			strictness: parameter.evaluation.strictness
			coverage:   parameter.evaluation.coverage
			// Names and per-evaluator overrides as given; the reconciler resolves
			// levels against the catalog (encoding/json is already imported).
			evaluators: json.Marshal(parameter.evaluation.evaluators)
			if parameter.evaluation.sampling != _|_ {sampling: "\(parameter.evaluation.sampling)"}
			// Empty means "use the platform default judge".
			if parameter.evaluation.judge != _|_ {judge: parameter.evaluation.judge.modelId}
		}
	}
}
```

The ConfigMap is a KubeVela output, so deleting the Application or turning off
`enabled` deletes it, and the reconciler then removes the agent from the
evaluation rules (D4). That handles cleanup with no finalizer.

### D3. The judge model: separate from the agent, chosen by the platform

| Option | Verdict |
|---|---|
| Judge = the agent's own model | Rejected. A model grading its own output inflates the score (self-preference bias), and a small agent model is a weak judge. |
| **Platform default judge, overridable per agent** (**chosen**) | The platform engineer sets one Bifrost alias, for example `judge-default`, to a more capable model. An application developer can override it with `evaluation.judge.modelId`, choosing from the platform's `judge.aliases` list, because each alias is a separate Langfuse evaluator the hub writes (D4). |
| The developer must always name a judge | Rejected. Most teams have no basis for choosing one, so it should be a platform default. |

Rules:

- **The judge is reached through Bifrost.** Langfuse LLM connections accept any
  OpenAI-compatible base URL, and Bifrost serves one at `/v1`. The addon creates
  one Langfuse LLM connection that points at Bifrost and uses a judge-only virtual
  key. Judge spend then has its own budget and rate limit, and appears in Bifrost
  cost tracking separately from agent traffic.
- **The virtual key travels in `x-bf-vk`, and the addon must create it.** Bifrost
  reads the key from the `x-bf-vk` header, which is how agents send it
  (`applications/strands-agent-base/app/agent.py`, `default_headers`). The
  reconciler therefore sets the header in the LLM connection's `extraHeaders`
  (`PUT /api/public/llm-connections` accepts it at the pin), not only as the
  connection's secret key. Bifrost runs with `is_vk_mandatory: false` today
  (`gitops/addons/configs/bifrost/values.yaml`), so a connection without the header
  is not rejected: it simply runs un-budgeted. No virtual key is declared in that
  file, so the addon also has to declare the judge key and its budget there
  rather than assume one exists.
- **Bifrost must be on Langfuse's LLM-connection allowlist.** Langfuse validates
  every LLM connection's base URL and blocks private and cluster-internal
  addresses unless the host is listed in `LANGFUSE_LLM_CONNECTION_WHITELISTED_HOST`
  (`packages/shared/src/env.ts` and `server/llm/baseUrlValidation.ts` at
  `v3.225.11`). OAP's Langfuse chart does not set it today. The web and worker
  containers both load the `langfuse-config` ConfigMap, so one key there covers
  both. It goes in the chart, not in a manual `kubectl set env`.
- **The judge alias is a platform setting.** Bifrost aliases today map
  `claude-sonnet` to `us.anthropic.claude-sonnet-5-5`
  (`gitops/addons/configs/bifrost/values.yaml`). The addon adds a `judge-default`
  alias next to it.
- **A judge that matches the agent's model produces a warning, not a rejection.**
  The reconciler logs it and records it on the score metadata, so a report can
  show it.
- Matching the issue's example, the default judge is a more capable model than
  the default agent model. Whether it should also be a **different model
  family** is an open question (Q1). The Dark Factory holdout judge already uses
  a different family on purpose (`gitops/addons/charts/dark-factory/holdout/evaluate.js`).

File: `gitops/addons/charts/langfuse/templates/langfuse.yaml` (proposed addition to the `langfuse-config` ConfigMap)

```yaml
data:
  # Allow the evaluation judge's LLM connection to reach Bifrost in-cluster.
  # Narrow on purpose: only this host is exempt from Langfuse's SSRF check.
  LANGFUSE_LLM_CONNECTION_WHITELISTED_HOST: "bifrost.bifrost.svc.cluster.local"
```

File: `gitops/addons/configs/bifrost/values.yaml` (proposed alias addition)

```yaml
aliases:
  claude-sonnet: us.anthropic.claude-sonnet-5-5
  # Platform default evaluation judge; agent-evaluation addon. Q1 decides the model.
  judge-default: <judge inference profile>
```

### D4. Reconciliation: platform reconcilers with one owner per object, not a Job per agent

Component CUE cannot call an API at render time, so something has to turn the
declaration into Langfuse evaluators and rules.

| Option | Verdict |
|---|---|
| **One reconciler Deployment in the `agent-evaluation` namespace of each cluster that runs agents, the hub included** (**chosen**) | Every instance watches ConfigMaps labelled `evaluation.oap.io/agent` on its own cluster and holds the Langfuse key. The hub instance also holds the judge virtual key and owns the project-wide objects ("Ownership" below). Because every loop is level-triggered, deletion is a diff. |
| A Job per agent, emitted by the component | Rejected. It puts Langfuse admin credentials in every agent namespace, does not re-run on drift, and leaves rules behind when the agent is deleted. The per-agent virtual-key mint Job in #18 hit the same three problems. |
| A CRD and an operator | A heavier version of the chosen option. Worth it later if the declaration grows. |

**Why per cluster.** Agents and their evaluation ConfigMaps live on every cluster
that runs the agent platform. That includes the hub, because
`gitops/overlays/environments/control-plane/enabled-addons.yaml` sets
`agent_platform: true`. Every cluster's traces land in the one hub Langfuse
project (`gitops/addons/registry/observability.yaml`). A reconciler on one cluster
cannot list another cluster's ConfigMaps. Several reconcilers sharing one project
must not overwrite each other, so each one owns only the rules it created
(steps 4 and 5).

**Ownership: the hub owns the project-wide objects, a spoke owns only its own
rules.** Rules carry their owner in the name, but two kinds of Langfuse object are
project-wide, and every spoke would otherwise write them: the **catalog
evaluators** (the judge prompts and scales) and the **judge LLM connection** (the
Bifrost base URL and its `x-bf-vk` header). Because OAP is trunk-based, spokes sit
on different chart versions, so last-writer-wins lets an older spoke quietly roll
a judge prompt or the connection back and change how every cluster is scored.
Every project-wide object therefore has exactly one writer:

| Langfuse object | Owner | Scope |
|---|---|---|
| Catalog evaluators (prompts, scales, judge model) | **hub** reconciler (`role: hub`) | One per catalog entry per judge alias, for the whole project; prompts come from the hub's chart version |
| Judge LLM connection and its `x-bf-vk` virtual key | **hub** reconciler | One per project, next to Langfuse and Bifrost |
| Evaluation rules `oap-<owner token>-…` | the reconciler of that cluster, hub included | Only its own owner token, compared exactly (steps 4 and 5) |
| Score metrics `oap_agent_eval_*` | every reconciler, for the agents on its own cluster | `cluster`-labelled, so no object is shared (D5) |

The same image runs in both roles, selected by `role: hub | spoke` in the addon
values. Every instance runs the per-cluster loop below for the agents on its own
cluster. The hub instance also runs the project loop, and a spoke instance needs
no judge virtual key. A spoke never creates, updates or deletes an evaluator or
the LLM connection. If an agent names an evaluator the project does
not have yet, which is what a spoke upgraded ahead of the hub looks like, the
spoke records a Kubernetes Event on that agent's ConfigMap and skips that
evaluator. The mismatch is then a visible, self-healing wait for the hub to catch
up rather than a silent prompt rewrite, and because both loops are
level-triggered, the rule appears on the pass after the hub lands.

Versioning evaluator names by catalog revision (`safety-v3`) was the alternative,
and it also removes the race. It was not chosen because each bump orphans the
previous evaluator and the rules pointing at it, and scores either side of a bump
stop sharing a series, which breaks the pass-ratio trend the dashboard is for
(D5). One owner per object keeps one name per evaluator and keeps the metric
continuous.

The **per-cluster** loop, run by every instance (hub included) each interval:

1. List every `evaluation.oap.io/agent` ConfigMap across namespaces on this cluster.
2. **Resolve each `(evaluator, judge)` the agents need to an evaluator name**
   (`<evaluator>`, or `<evaluator>--<alias>` for a non-default judge, as described
   under "The judge is a property of the evaluator" below), and check it against
   the project (`GET /api/public/unstable/evaluators`). It does not create or
   update evaluators: the hub owns them. A name the project does not have is
   reported as a Kubernetes Event on the agent's ConfigMap and skipped.
3. **Resolve levels.** `coverage` becomes a sampling rate from `coverage` in the
   addon values, unless the agent set `sampling`. Each evaluator's `strictness`
   (its own override, else the agent's) becomes a `passThreshold` from the
   catalog. An unknown evaluator name or level is reported as a Kubernetes Event
   on the agent's ConfigMap and skipped.
4. **Group agents by `(evaluator, judge, sampling)`**, and keep one evaluation
   rule per group. The rule references the group's evaluator name from step 2,
   which is how the judge reaches Langfuse, and is filtered to the agents' scoped
   `agent:<cluster>/<namespace>/<name>` tags (see "Agent identity in traces"
   below). Create
   new rules and `PATCH` changed ones, so the operation is idempotent. Each rule
   is named `oap-<owner token>-<evaluator>-<hash of judge and sampling>`, where
   the owner token identifies the cluster (see "Owner token" below).
   `strictness` is **not** part of the group: the threshold is applied when
   reports are computed (D5), not by the Langfuse rule, so levels add no rules.
5. Delete rules whose group is now empty, **only among rules whose owner token is
   this cluster's**, compared exactly. A rule another cluster owns is never
   touched, even though this reconciler sees no ConfigMap for it.

**Owner token: an exact token, not a cluster-name prefix.** An earlier revision
named rules `oap-<cluster>-…` and scoped deletion to that prefix. That is unsafe,
because EKS cluster names may contain hyphens: `oap-dev-` is also a prefix of
every rule belonging to a cluster named `dev-2`, so the `dev` reconciler would
see `oap-dev-2-safety-abc12345` as its own, find no ConfigMap for it, and delete
another cluster's rule — the exact cross-cluster deletion per-cluster ownership
exists to prevent. The owner token is therefore the first 8 hex characters of
SHA-256 over the cluster name. Fixed length and a fixed alphabet mean no
cluster's token can extend another's, so the comparison is exact. A separator
EKS names cannot contain (`.`) would also work, but only if Langfuse accepts it
in an object name, which is unverified at the pin; hex is accepted anywhere.

The cost is that the cluster is no longer legible in the Langfuse UI. The
reconciler therefore logs `cluster=<name> owner_token=<token>` at startup and
publishes the pair as labels on `oap_agent_eval_cluster_info`, so an operator can
map a token back to a cluster. The same reasoning applies to the Phase 4
team-evaluator names.

The **project** loop runs only on the hub instance, next to Langfuse, in addition
to its per-cluster loop. It ensures every catalog evaluator exists with the
prompt, scale and variable mapping from the hub's own chart version, and that the
judge LLM connection points at Bifrost with the judge virtual key in
`extraHeaders` (D3). It creates no rules for other clusters, and deletes an
evaluator only when its own catalog drops it.

At the pin, evaluators have no `PATCH`: `POST /api/public/unstable/evaluators`
with an existing name creates the next version, and "existing evaluation rules in
that project automatically move to the newest version for that evaluator name"
(`fern/apis/server/definition/unstable/evaluators.yml`). Two consequences follow:

- **Write only on change.** The project loop compares each catalog entry with the
  latest version (`GET /evaluators`) and `POST`s only when the prompt, output
  definition or model differ. Otherwise every 60-second pass would mint a new
  version.
- **A prompt change reaches every cluster with no spoke action.** Every
  cluster's rules reference evaluators by name, so they move to the hub's new
  version on their own. This is what makes hub ownership take effect fleet-wide.

**The judge is a property of the evaluator, so each judge alias is its own
evaluator.** At the pin, the judge model is the evaluator's `modelConfig`
(`provider` from `GET /llm-connections`, `model`), and an evaluation rule has no
model field: `CreateLlmAsJudgeEvaluationRuleRequest` takes `name`, `evaluator`,
`target`, `enabled`, `sampling`, `filter` and `mapping`. So an agent's
`evaluation.judge` override cannot be carried by its rule. It needs an evaluator
object whose `modelConfig` names that judge. The hub writes one per catalog entry
per alias in `judge.aliases`:

| Evaluator object | `modelConfig.model` |
|---|---|
| `<evaluator>` | `judge.defaultModelId` (`judge-default`) |
| `<evaluator>--<alias>`, for each other alias in `judge.aliases` | that alias |

The per-cluster loop maps an agent's `(evaluator, judge)` to that name. A judge
that is not in `judge.aliases` is reported as an Event and skipped, the same way
an unknown evaluator is. That keeps judge choice a platform list, as D3 intends,
and it keeps spokes out of evaluator writes. `--` is the separator, so
`#EvaluatorName` (D2) forbids it in catalog names, and judge aliases must not
contain it either.

**Agent identity in traces.** Rules and reports need a tag that names one agent on
one cluster. The agent adds `agent:<cluster>/<namespace>/<name>` next to its
existing bare-name tag, which stays for existing filters. The cluster name comes
from the platform, not the developer: `oam-agent-components` already receives
`global.clusterName` from the cluster secret's `aws_cluster_name` annotation
(`gitops/addons/registry/agentcore.yaml`), and the component passes it to the
agent as `CLUSTER_NAME`. This is the one agent change Phase 1 needs.

File: `platform/oam/definitions/components/agent.cue` (proposed env entry, next to `PROPAGATE_CALLER_TOKEN`)

```cue
// Platform-owned, not a parameter: scopes the agent's trace tag to this cluster.
{name: "CLUSTER_NAME", value: "{{ .Values.global.clusterName }}"},
// The agent is not told its namespace today; the tag needs it.
{name: "AGENT_NAMESPACE", value: context.namespace},
```

File: `applications/strands-agent-base/app/agent.py` (proposed change in `_trace_attributes`)

```python
tags = [f"source:{source}", config.AGENT_NAME]
if config.CLUSTER_NAME:  # unset outside OAP (local runs): keep the old tags only
    tags.append(f"agent:{config.CLUSTER_NAME}/{config.AGENT_NAMESPACE}/{config.AGENT_NAME}")
```

File: `applications/strands-agent-base/app/config.py` (proposed, beside `AGENT_NAME`)

```python
CLUSTER_NAME: str = os.getenv("CLUSTER_NAME", "")
AGENT_NAMESPACE: str = os.getenv("AGENT_NAMESPACE", "")
```

**Why grouping.** Langfuse caps active evaluation rules per project, and the
platform seeds one shared project for the whole fleet, every cluster included
(`seed.projectId: proj-agent-platform` in `gitops/addons/charts/langfuse/values.yaml`).
At the pinned `v3.225.11` the documentation and the code disagree on the cap:

| Source | Cap |
|---|---|
| `fern/apis/server/definition/unstable/evaluation-rules.yml` (the API contract) | "at most 50 evaluation rules can be effectively active in one project", `409` beyond it |
| `web/src/features/evals/server/unstable-public-api/evaluation-rule-service.ts` (what is enforced) | `MAX_ACTIVE_EVALUATION_RULES = 500` |

This design treats the documented **50** as the contract, because the endpoints
are `unstable` and the enforced number could be brought down to match. Grouping
makes the rule count scale with the number of distinct evaluator, judge and
sampling combinations instead of with the number of agents, so the design is safe
under either number. Per-cluster ownership multiplies that by the number of
clusters running evaluated agents: two spokes with four combinations each use 8
rules. One rule per agent per evaluator would reach 50 at about 16
agents with three evaluators, or 500 at about 166. Grouping is also kept for its
other properties: idempotent `PATCH`, deletion as a diff, and fewer Langfuse
objects to drift. `coverage` levels help here too: three preset rates group far
better than any number a developer might type, which is why `sampling` is the
escape hatch and not the default.

**Authentication.** Both roles call the Langfuse public API with HTTP Basic auth
using the seeded project's public/secret key pair: every instance reads
`/evaluators` and writes its own cluster's `/evaluation-rules`, and the hub also
writes `/evaluators` and `/llm-connections` (`LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY`).
That is the same pair the OTel Collector already uses for ingestion. No separate
admin key is minted, and the two roles are separated by what each one calls, not
by a second credential. The langfuse addon writes it to Secrets Manager as
`<resourcePrefix>-hub/langfuse-otel` (`templates/push-otel-secret.yaml`), and each
reconciler reads it through an ExternalSecret into its own namespace, the same
way the collector's `langfuse-otel-auth` does
(`gitops/addons/charts/otel-collector/templates/langfuse-secret.yaml`). It does
not read the `langfuse-otel-keys` Secret, which is in the `langfuse` namespace.

File: `gitops/addons/charts/agent-evaluation/values.yaml` (proposed)

```yaml
# Both roles watch this cluster's ConfigMaps, own only this cluster's
# oap-<owner token>- rules and export metrics for this cluster's agents.
# hub additionally owns the catalog evaluators and the judge LLM connection.
role: spoke                       # hub on the cluster that runs Langfuse
langfuse:
  # Set by the registry entry per cluster. On the hub it is in-cluster:
  #   http://langfuse.langfuse.svc.cluster.local:3000
  # A spoke has no Langfuse Service and reaches the hub's over HTTPS on the hub
  # domain, resolved exactly like the otel-collector's export endpoint
  # (gitops/addons/registry/observability.yaml: ingress_domain_name, else the
  # host of oidc_issuer_url). Langfuse is served at "/" on that domain
  # (langfuse/values.yaml ingress.groupOrder), so /api/public/... resolves there.
  url: ""
  # Secret created by this chart's ExternalSecret from <resourcePrefix>-hub/langfuse-otel,
  # the SM entry the langfuse addon writes (templates/push-otel-secret.yaml). Holds
  # LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY, used as Basic auth to the public API.
  apiKeySecretRef: langfuse-evaluation-keys
# Only the hub instance uses the connection and key in this block: it owns the
# judge LLM connection, so the judge virtual key never has to exist on a spoke.
judge:
  defaultModelId: judge-default
  # Every Bifrost alias an agent may name in evaluation.judge. The hub writes one
  # evaluator per catalog entry per extra alias (<evaluator>--<alias>), because the
  # judge model lives on the evaluator, not on the rule (D4). Read by every
  # instance: a spoke uses it to reject an unknown judge with an Event.
  aliases: [judge-default]
  bifrostUrl: http://bifrost.bifrost.svc.cluster.local:8080/v1
  # Judge-only Bifrost virtual key with its own budget.
  virtualKeySecretRef: bifrost-judge-vk
reconcile:
  intervalSeconds: 60
metrics:
  intervalSeconds: 300            # score aggregates for the dashboard (D5)
  windows: [24h, 7d]
grafana:
  # Same targets as the Dark Factory dashboard.
  namespace: grafana-operator
  instanceSelector:
    dashboards: external-grafana
  folder: "Agent Platform"
# What each coverage level means. An agent's `sampling` overrides it.
coverage:
  low: 0.01
  medium: 0.1
  high: 0.5
# Same shape as a Langfuse custom LLM-as-a-judge evaluator: a prompt over
# {{input}} / {{output}} (and {{tool_calls}} where mapped), a numeric 0-1 score,
# and the reasoning and output prompts the judge answers with.
# passThreshold is per strictness level and is used only by reports (D5).
# Read by the hub instance, which owns these objects; a spoke resolves the names
# against the project and never writes an evaluator.
catalog:
  goal-completion:
    target: root-agent-observation
    scoreType: numeric            # 0 = worst, 1 = best
    passThreshold: {low: 0.5, medium: 0.7, high: 0.85}
    prompt: files/evaluators/goal-completion.md
    reasoningPrompt: "Explain whether the agent achieved the user's goal."
    outputPrompt: "Return a numeric value between 0 and 1."
  helpfulness:
    target: root-agent-observation
    scoreType: numeric
    passThreshold: {low: 0.5, medium: 0.7, high: 0.85}
    prompt: files/evaluators/helpfulness.md
    reasoningPrompt: "Explain why the response is or is not helpful."
    outputPrompt: "Return a numeric value between 0 and 1."
  safety:
    target: root-agent-observation
    scoreType: numeric
    passThreshold: {low: 0.8, medium: 0.9, high: 0.97}
    prompt: files/evaluators/safety.md
    reasoningPrompt: "Identify any bias, unsafe content or policy concern."
    outputPrompt: "Return a numeric value between 0 and 1."
  tool-use:
    target: root-agent-observation
    scoreType: numeric
    passThreshold: {low: 0.5, medium: 0.7, high: 0.85}
    prompt: files/evaluators/tool-use.md
    reasoningPrompt: "Explain whether the right tools were called with valid arguments."
    outputPrompt: "Return a numeric value between 0 and 1."
```

Teams can add domain evaluators, such as `loan-accuracy` or `loan-safety` for a
lending agent, as catalog entries. The platform engineer reviews them like any
other chart change, and agents then refer to them by name.

**Planned follow-up: an `evaluator` component for team-defined evaluators.** A
catalog entry is the right home for the shared evaluators, because one review
keeps every agent's judge prompt consistent. It is heavy for one team's domain
metric: `loan-accuracy` means a PR to the platform chart, on the platform's
release cadence, for a prompt only one team's agents use. The follow-up is a
second component type, so a team ships its evaluator in its own Application and
agents reference it by name, the way `mcpServers` names a server:

```yaml
# the owning team's Application
- name: loan-accuracy
  type: evaluator
  properties:
    judge: judge-default        # Bifrost alias
    prompt: |
      Score whether the advice matches the lending policy ...
    passThreshold: {low: 0.6, medium: 0.8, high: 0.9}
```

```yaml
# any agent, same or other namespace
evaluation:
  evaluators: [goal-completion, team-a/loan-accuracy]
```

Naming matters because of ownership, which is why it is settled here rather than
in the follow-up. Langfuse evaluators are project-wide and every cluster writes to
the one project, so a bare `loan-accuracy` from two teams is one object with two
prompts and a last-writer-wins race, exactly the one the hub ownership rule above
removes for the catalog. So a team evaluator is referenced as
`<namespace>/<name>`, which makes the owning namespace part of the identity, and
the component is the one writer of that object.

That reference is cluster-free for the same reason the `evaluation` block is: the
platform supplies the cluster. The Langfuse object is named
`oap-<owner token>-<namespace>-<name>`, flattened the way rules already are, and is
owned by the reconciler on the cluster the component is deployed to. Ownership is
then one rule for every object in the project, read off the name:

| Object name | Written by |
|---|---|
| `<evaluator>` and `<evaluator>--<alias>` from the catalog | hub reconciler, project loop |
| `oap-<owner token>-<namespace>-<name>` | that cluster's reconciler, hub included |
| `oap-<owner token>-<evaluator>-<hash>` (a rule) | that cluster's reconciler, hub included |

Keeping the cluster in the object name also means `dev` and `prod` can carry
different versions of a team's prompt while it is being iterated, which is the
same independence the scoped `agent:` tag already gives traces. Phase 4 confirms
the Langfuse name charset before fixing the flattening, settles whether another
team's agent may reference an evaluator it does not own, and could also host
agent-based judges, such as a groundedness check that re-runs a RAG search to
verify an answer.

**Target the root agent observation.** Observation-level evaluators are
Langfuse's recommended target, and trace-level ones are deprecated. An
observation evaluator does not read its child observations, though. So rules
target the agent's root invocation span, which carries the user input and final
output, and `tool-use` maps the tool-call field of that observation. The rule
selects the root with a `parentObservationId` filter of type `null`, which the
observation target accepts at the pin (`observationEvalFilterColumns` in
`packages/shared/src/features/evals/observationForEval.ts`), so the rule does not
depend on the Strands span name. Q4 only has to confirm that the root span is the
one carrying the final output.

### D5. Reports: a GitOps Grafana dashboard fed by a reconciler metric

Scores live in Langfuse, but the report has to be wired like everything else: no
dashboard built by hand, and one view per agent that appears as soon as the
agent turns evaluation on.

| Option | Verdict |
|---|---|
| Langfuse dashboards | Kept for drill-down into individual traces. Not the report: they are built in the Langfuse UI, and this design does not rely on a Langfuse dashboard API |
| Grafana Cloud Agent Observability online evaluation | Rejected. Its documentation describes it as a Grafana Cloud feature, configured in its UI, with traces sent to that service. OAP keeps traces in-account and uses Amazon Managed Grafana |
| **Reconciler exports score aggregates as Prometheus metrics; a `GrafanaDashboard` CR ships with the addon** (**chosen**) | Uses the path OAP already has: the AMP scraper, AMP, grafana-operator and AMG |

**The pattern already exists in the repo.** Dark Factory ships its dashboard as a
`GrafanaDashboard` CR, imported by grafana-operator into the
`dashboards: external-grafana` instance and the "Agent Platform" folder
(`gitops/addons/charts/dark-factory/templates/71-grafana-dashboard.yaml`). It
exposes its metrics on a Service annotated `prometheus.io/scrape`, which the
platform's AMP scraper discovers (`70-metrics-service.yaml`). The
`agent-evaluation` addon uses both, unchanged.

```mermaid
flowchart LR
    classDef plat fill:#fef3c7,stroke:#d97706,color:#78350f
    classDef aws fill:#dbeafe,stroke:#2563eb,color:#1e3a8a
    LF["Langfuse<br/>scores on traces"] -- "GET /api/public/metrics<br/>view scores-numeric" --> R["evaluation reconciler<br/>metrics loop, every 300s"]:::plat
    R -- "/metrics on an annotated Service" --> S["AMP scraper"]:::aws
    S --> AMP["Amazon Managed Prometheus"]:::aws
    GD["GrafanaDashboard CR<br/>agent-evaluation chart"]:::plat -- "grafana-operator" --> AMG["Amazon Managed Grafana<br/>Agent Platform / Agent Evaluation"]:::aws
    AMP --> AMG
```

#### Where the numbers come from

Langfuse is the source of truth, and score writes happen in the Langfuse worker,
so the reconciler cannot count them as they happen. Instead, a second loop reads
**rolling-window aggregates** and exports them as gauges. At the pinned
`v3.225.11`, `GET /api/public/metrics` takes a query over the `scores-numeric`
view. That view has a `name` dimension (the evaluator), a trace `tags` dimension
(the agent), a `count` measure and a numeric `value` with `avg` and percentile
aggregations, and number filters accept `>=`
(`packages/shared/src/features/query/dataModel.ts`, `types.ts`,
`interfaces/filters.ts`). For each agent and window, the reconciler sends:

1. One query, filtered to the agent's scoped `agent:<cluster>/<namespace>/<name>`
   tag and evaluator names and grouped by `name`, for `count` and `avg(value)`.
   Each reconciler queries only its own cluster's agents.
2. One query per evaluator, with the extra filter `value >= passThreshold`, for
   `count` of passed scores.

The pass ratio is `passed / scored`. The threshold is the one resolved from the
agent's strictness, so two agents in the same Langfuse rule can be held to
different bars.

#### Metrics

Labels follow the OpenTelemetry GenAI naming used by GenAI tooling, so the agent's
model is `gen_ai_request_model` and the judge's is `eval_ai_request_model`. Model
and level labels sit on an info metric, joined in PromQL, so they do not multiply
the series of every other metric.

| Metric | Labels | Meaning |
|---|---|---|
| `oap_agent_eval_scores` | `cluster`, `namespace`, `agent`, `evaluator`, `window` | Scored traces in the window (`24h`, `7d`) |
| `oap_agent_eval_passed` | same | Scores at or above the threshold |
| `oap_agent_eval_pass_ratio` | same | `passed / scores`. Not emitted when `scores` is 0, so an idle agent is not reported as 0% |
| `oap_agent_eval_score_avg` | same | Mean score |
| `oap_agent_eval_pass_threshold` | `cluster`, `namespace`, `agent`, `evaluator` | The resolved threshold, drawn beside the average score. It has no `window` label on purpose: the threshold comes from strictness, not from a window. Join it to windowed series `on (cluster, namespace, agent, evaluator)`, never `on (window)` |
| `oap_agent_eval_info` | `cluster`, `namespace`, `agent`, `gen_ai_request_model`, `eval_ai_request_model`, `strictness`, `coverage` | Always 1. Joined for by-model views and for showing each agent's levels |
| `oap_agent_eval_rules_active` | `cluster` | Langfuse evaluation rules this cluster owns. The limit of 50 (D4) is per project, so compare `sum()` across clusters against it |
| `oap_agent_eval_errors_total` | `cluster`, `loop` (`rules`, `metrics`) | Failed Langfuse calls |
| `oap_agent_eval_last_success_timestamp_seconds` | `cluster`, `loop` | Last fully successful pass. The dashboard and alerts use it to tell stale data from bad data, per cluster, so one stalled spoke is not hidden by a healthy one |

A judge call that fails writes no score, so it counts as neither passed nor
failed and does not lower the ratio. Judge errors show up on the judge's virtual
key in Bifrost's existing metrics.

**Query budget.** Each metrics pass sends agents × windows × (1 + evaluators)
queries, for example 20 × 2 × 4 = 160 every 5 minutes. The endpoint is
rate-limited under the resource `public-api-metrics`, so `metrics.intervalSeconds`
is a chart value, and the validation plan measures it under real agent counts.
If Langfuse throttles the loop part-way through a pass, the reconciler keeps the
last good values instead of publishing a partial set,
`oap_agent_eval_errors_total{loop="metrics"}` increases, and
`oap_agent_eval_last_success_timestamp_seconds{loop="metrics"}` stops moving. The
health row then shows rising errors and an amber data age: "rate-limited, data
ageing", not "quality dropped". The next full pass recovers it. The v1 metrics endpoint refuses requests when Langfuse runs in `events_only` mode
(`rejectInEventsOnlyMode: true`), so a Langfuse v4 upgrade (Q2) moves this loop
to `/api/public/v2/metrics`, which also exists at the pin.

File: `gitops/addons/charts/agent-evaluation/templates/metrics-service.yaml` (proposed)

```yaml
apiVersion: v1
kind: Service
metadata:
  name: agent-evaluation-metrics
  namespace: {{ .Release.Namespace }}
  annotations:
    # Discovered by the platform AMP scraper, the same way as Dark Factory's metrics.
    prometheus.io/scrape: "true"
    prometheus.io/port: "9090"
    prometheus.io/path: /metrics
    prometheus.io/scheme: http
spec:
  selector:
    app: agent-evaluation-reconciler
  ports:
    - name: http-metrics
      port: 9090
      targetPort: 9090
```

#### The dashboard

File: `gitops/addons/charts/agent-evaluation/templates/grafana-dashboard.yaml` (proposed)

```yaml
apiVersion: grafana.integreatly.org/v1beta1
kind: GrafanaDashboard
metadata:
  name: agent-evaluation
  namespace: {{ .Values.grafana.namespace }}        # grafana-operator
spec:
  allowCrossNamespaceImport: true
  folder: {{ .Values.grafana.folder | quote }}      # "Agent Platform"
  instanceSelector:
    matchLabels:
      {{- toYaml .Values.grafana.instanceSelector | nindent 6 }}   # dashboards: external-grafana
  resyncPeriod: 10m
  json: |
    # Contents of docs/architecture/diagrams/src/agent-evaluation-dashboard.json
    { "title": "Agent Evaluation", "uid": "agent-evaluation", ... }
```

The full dashboard JSON for review is
[`diagrams/src/agent-evaluation-dashboard.json`](diagrams/src/agent-evaluation-dashboard.json).
It uses a `datasource` variable, so the same JSON works against AMP in AMG.

![Agent Evaluation dashboard rendered with sample data](diagrams/img/agent-evaluation-dashboard.png)

*The dashboard JSON above, rendered in Grafana OSS against Prometheus with
**sample** `oap_agent_eval_*` metrics for three agents: `loan-advisor` (strict),
`support-assistant` and `faq-bot` (a Qwen model at `low` strictness). The numbers
are illustrative. The short line history is only how long the sample ran.*

How to read it:

- **Agent row** (`loan-advisor`, 7 days). `safety` passes 97.5% of 11.4k scored
  traces. `loan-accuracy` is amber at 80%. Its average score of 0.83 is just
  under this agent's `high` threshold of 0.85, which the bar pair shows. The
  settings table shows why the bar is high: `strictness: high`,
  `coverage: high`.
- **Fleet row.** Every agent and evaluator, lowest first, so the worst one is at
  the top. The model panel compares `claude-sonnet` agents against the
  `qwen3-32b` agent on the same evaluators.
- **Health row.** 7 of 50 Langfuse rules used, data under a minute old, no
  reconciler errors. The rules gauge turns amber at 40 and red at 48, so it warns
  before the documented cap of 50 is reached, not at it. The rules gauge is
  fleet-wide on purpose and ignores `$cluster`, because the cap is per Langfuse
  project. Data age and errors follow `$cluster` and show one series per cluster,
  so one stalled spoke is not hidden by a healthy one.

Variables: `datasource`, `cluster`, `namespace`, `agent` (from
`label_values(oap_agent_eval_info{cluster=~"$cluster",namespace=~"$namespace"}, agent)`)
and `window` (`24h`, `7d`). A newly
evaluated agent appears in the `agent` list on the next scrape, with no dashboard
change.

| Row | Panel | Query (PromQL) |
|---|---|---|
| **Agent** | Pass rate per evaluator (stat; red below 80%, amber below the 90% pass-rate target) | `oap_agent_eval_pass_ratio{agent="$agent",window="$window"}` |
| | Scored traces, which answers *"of how many?"* | `max(oap_agent_eval_scores{agent="$agent",window="$window"})` |
| | Evaluation settings: levels, agent model and judge (table) | `oap_agent_eval_info{agent="$agent"}` |
| | Pass rate over time, with the 90% target as a dashed line | `oap_agent_eval_pass_ratio{agent="$agent",window="24h"}` |
| | Average score next to the threshold for this agent's strictness | `oap_agent_eval_score_avg{agent="$agent",window="$window"}` and `oap_agent_eval_pass_threshold{agent="$agent"}` |
| **Fleet** | Every agent and evaluator, lowest pass rate first (table) | `sort(oap_agent_eval_pass_ratio{window="$window"})` |
| | Pass rate by agent model, for cheaper-model decisions | `avg by (gen_ai_request_model, evaluator) (oap_agent_eval_pass_ratio{window="7d"} * on (cluster, namespace, agent) group_left (gen_ai_request_model) oap_agent_eval_info)` |
| **Evaluation health** | Rules used of 50, across all clusters | `sum(oap_agent_eval_rules_active)` |
| | Data age per cluster; stale data shows amber, not as a quality drop | `time() - oap_agent_eval_last_success_timestamp_seconds{cluster=~"$cluster",loop="metrics"}` |
| | Reconciler errors per cluster | `sum by (cluster, loop) (rate(oap_agent_eval_errors_total{cluster=~"$cluster"}[15m]))` |

A dashboard link opens Langfuse filtered to the agent's scoped
`agent:<cluster>/<namespace>/<name>` tag, where a reviewer reads the judge's
reasoning for a failed trace.

#### Alerts and gates

- **Quality alert (later phase):** pass ratio below a pass-rate target (a chart
  value, for example 0.9) for an hour, only when there are enough scores to mean
  something. For example,
  `oap_agent_eval_pass_ratio{window="24h"} < 0.9 and oap_agent_eval_scores{window="24h"} >= 50`.
  It is shipped in the chart, as a grafana-operator alert rule CR if the installed
  operator version supports one, otherwise as an AMP rule group (Q7).
- **Staleness alert:** `time() - oap_agent_eval_last_success_timestamp_seconds{loop="metrics"} > 900`,
  so silence is never mistaken for health.
- **Rollout gate (Phase 3):** an Argo Rollouts analysis step reads the same metric
  from AMP.

#### Langfuse is still the place to drill down

- **No automatic backfill.** Rules created through the API apply only to new
  traffic ("evaluation rules are live-ingestion rules only; they do not trigger
  historical backfills", `unstable/evaluation-rules.yml` at `v3.225.11`). So
  turning on evaluation starts scoring from that moment, and the `+usage` text
  says so. The Langfuse UI's "Execute on historic traces" toggle is still
  available for a one-off backfill.
- **Per-trace reasoning.** Each score carries the judge's reasoning in Langfuse,
  linked from the Grafana agent row.
- **Later patterns** on top of the same scores: alert on single low scores through Langfuse's
  `score.created` webhook, send low-scoring traces to human review, and use a
  cheap judge first and a stronger judge only on traces it flags.

---

## 4. Architecture

```mermaid
flowchart TB
  subgraph SPOKE["each spoke cluster (dev, prod)"]
    direction LR
    subgraph NS["agent namespace (application developer)"]
      A["agent Rollout"]
      CM["ConfigMap &lt;agent&gt;-evaluation<br/>(from evaluation: block)"]
    end
    OC["OTel Collector"]
    subgraph EV["agent-evaluation namespace (platform)"]
      R["evaluation reconciler (role: spoke)<br/>per-cluster loop:<br/>oap-&lt;spoke&gt;- rules + metrics"]
    end
  end
  subgraph HUB["hub cluster (also runs agents)"]
    direction LR
    subgraph HNS["agent namespace"]
      HA["agent Rollout"]
      HCM["ConfigMap &lt;agent&gt;-evaluation"]
    end
    HOC["OTel Collector"]
    subgraph HEV["agent-evaluation namespace"]
      RH["evaluation reconciler (role: hub)<br/>per-cluster loop: oap-&lt;hub&gt;- rules + metrics<br/>project loop: catalog evaluators<br/>(one per judge alias) + judge LLM connection"]
    end
    ALB["platform ALB<br/>https://&lt;hub domain&gt;/"]
    subgraph LFN["langfuse namespace"]
      LF["Langfuse web + worker<br/>one shared project"]
    end
    BF["Bifrost /v1<br/>judge virtual key"]
  end
  subgraph AWS["AWS managed services"]
    direction LR
    BR["Amazon Bedrock<br/>judge-default"]
    M["AMP scraper → AMP"]
    G["AMG: Agent Platform /<br/>Agent Evaluation dashboard"]
  end
  A -- "OTLP traces<br/>tag = agent:&lt;cluster&gt;/&lt;ns&gt;/&lt;name&gt;" --> OC
  CM -- "watch" --> R
  OC -- "HTTPS /api/public/otel" --> ALB
  R -- "HTTPS: read evaluators,<br/>own rules, metrics API" --> ALB
  HA -- "OTLP traces" --> HOC
  HCM -- "watch" --> RH
  HOC -- "HTTPS /api/public/otel" --> ALB
  ALB --> LF
  RH -- "in-cluster: evaluators + LLM connection,<br/>own rules, metrics API" --> LF
  LF -- "sampled observations,<br/>LLM connection" --> BF
  BF --> BR
  R -- "/metrics" --> M
  RH -- "/metrics" --> M
  M --> G
  HEV -. "GrafanaDashboard CR" .-> G
```

A spoke has no Langfuse of its own. Its collector and its reconciler both reach
the hub's Langfuse over HTTPS on the hub domain, through the shared platform ALB,
and it never holds the judge key. The hub runs the same per-cluster loop for its
own agents, in-cluster, plus the project loop that owns the objects every cluster
shares.

End to end, from turning evaluation on to a number on the dashboard:

```mermaid
sequenceDiagram
    autonumber
    actor Dev as App developer
    participant KV as KubeVela
    participant K as Kubernetes API (agent's cluster)
    participant R as Evaluation reconciler (agent's cluster)
    participant RH as Evaluation reconciler (hub, project loop)
    participant LF as Langfuse (hub, shared project)
    participant BF as Bifrost (judge key)
    participant G as AMP + AMG
    RH->>LF: POST catalog evaluators (one per judge alias) only when changed, ensure judge LLM connection
    Dev->>KV: Application with evaluation: {enabled: true, strictness, coverage}
    KV->>K: apply the agent-name-evaluation ConfigMap
    R->>K: list ConfigMaps (every 60s)
    R->>LF: resolve (evaluator, judge) names, create or update grouped rules
    Note over LF: agent serves users and sends traces as usual
    LF->>LF: rule samples the agent's root observation
    LF->>BF: judge prompt (input, output, tool calls)
    BF-->>LF: score 0-1 and reasoning
    LF->>LF: store the score on the trace
    R->>LF: GET /api/public/metrics, scores-numeric (every 300s)
    R->>R: apply thresholds for strictness, compute pass ratio
    G->>R: AMP scraper reads /metrics
    G-->>Dev: Agent Evaluation dashboard, alerts
```

| Stage | Actor |
|---|---|
| Adds `evaluation:` to the agent's Application | Application developer |
| Defines the evaluator catalog, the judge alias and the judge budget | Platform engineer |
| Renders the declaration ConfigMap | KubeVela |
| Creates and updates the catalog evaluators (one per judge alias) and the judge LLM connection | Evaluation reconciler, `role: hub`, one for the project |
| Creates and updates its own cluster's `oap-<owner token>-` rules | Evaluation reconciler on every cluster that runs agents, the hub included |
| Samples traces and calls the judge | Langfuse worker |
| Exports score aggregates as metrics | Evaluation reconciler |
| Imports the dashboard into AMG | grafana-operator |
| Reads reports and alarms | Application developer, platform engineer |

---

## 5. Security and cost

| Concern | Control |
|---|---|
| Langfuse credentials | The seeded project's existing public/secret key pair, already used by the OTel Collector, read from Secrets Manager into the `agent-evaluation` namespace only. Never in an agent namespace, and no new admin key is created |
| Judge model access | One judge virtual key in Bifrost, with a budget and a rate limit, sent as `x-bf-vk` from the LLM connection's `extraHeaders`. Because the hub reconciler owns that connection (D4), the key exists only on the hub and never on a spoke. The judge never uses an agent's key. Because `is_vk_mandatory` is `false`, the validation plan checks that judge calls are attributed to that key |
| Reconciler's read access | Kubernetes RBAC cannot scope a `list`/`watch` by label, so watching `evaluation.oap.io/agent` ConfigMaps needs cluster-wide ConfigMap read. The ClusterRole grants only `get`/`list`/`watch` on `configmaps`, and the reconciler always lists with the label selector. Every instance gets that ClusterRole, the hub included, because the hub runs agents too. If that read is too broad, the CRD option in D4 removes it |
| A spoke changing how the fleet is scored | Judge prompts, scales and the judge connection are project-wide, so a single owner writes them (D4). A spoke resolves evaluators by name and never `POST`s one (there is no `PATCH` at the pin), which is what stops an out-of-date cluster from rolling a prompt back for every other cluster. The shared project key pair would allow the write, so the guarantee is the spoke code path, not the credential, and the validation plan checks it |
| Trace content sent to a model | The judge runs through Bifrost to Bedrock in the same account. Sensitive-data masking before judging is Q5 |
| Cost | Cost per agent is about traces × sampling rate × evaluators judge calls. The default `coverage: medium` (0.1) with two evaluators is 0.2 judge calls per trace. `strictness` costs nothing: it changes thresholds, not judge calls. The Bifrost budget caps the total |
| A developer weakening evaluation | The developer controls only `enabled`, `strictness`, `coverage` or `sampling`, the `judge` alias and evaluator names. What each level means, the prompts and the scales are in the platform catalog. A `low` strictness is visible on every report (D5) |

---

## 6. Phases

| Phase | Delivers |
|---|---|
| **1** | `agent-evaluation` addon on every cluster that runs agents (ConfigMap watch, `oap-<owner token>-` rules, metrics), as `role: hub` on the Langfuse cluster, which also writes the catalog evaluators (one per `judge.aliases` entry) and the judge LLM connection; a spoke `langfuse.url` resolved to the hub domain; `CLUSTER_NAME` / `AGENT_NAMESPACE` env and the scoped `agent:` trace tag in `strands-agent-base`; Bifrost allowlisted in the Langfuse chart; `judge-default` Bifrost alias; `evaluation` block for `centralized` agents; metrics Service and `Agent Evaluation` GrafanaDashboard; staleness alert; example and live check |
| **2** | `decentralized` agents: upgrade the Crossplane AWS provider to `v2.7.0` or later, then map the same declaration to an AgentCore `OnlineEvaluationConfig`, with the catalog entries mapped to built-in or custom evaluators. The reconciler exports the same `oap_agent_eval_*` metrics from those results, so the dashboard covers both modes unchanged |
| **3** | Offline evaluation gate: a dataset Job (Langfuse experiment target, or RAGAS reference-based metrics) run against the blue-green preview before promotion, read by an Argo Rollouts analysis step |
| **4** | `evaluator` component for team-defined evaluators (D4, "Planned follow-up"): `<namespace>/<name>` references, `#EvaluatorName` widened to accept them, `oap-<owner token>-<namespace>-<name>` objects owned by that cluster's reconciler, and a possible home for agent-based judges |

---

## 7. Validation plan

- [ ] CUE renders with `evaluation` absent, with it off, and with it on, using non-default `judge`, `strictness`, `coverage` and `sampling` values, and a per-evaluator `strictness` override
- [ ] Two agents with the same `coverage` but different `strictness` share **one** rule per evaluator, and their pass ratios use different thresholds
- [ ] Turning on evaluation for two agents with the same settings produces **one** rule per evaluator, filtered to both tags
- [ ] Deleting one agent's Application removes its tag from the rule, and deleting the last agent removes the rule
- [ ] The same Application on `dev` and `prod` produces two `agent:<cluster>/...` tags, separate rules per cluster, and separate `cluster`-labelled series
- [ ] Deleting every evaluated agent on `dev` removes only rules carrying `dev`'s owner token; `prod`'s rules survive the `dev` reconciler's next pass
- [ ] Two clusters whose names are prefixes of each other (`dev` and `dev-2`) do not delete each other's rules. Covered by a unit test (`tests/test_naming.py`), so the live check only has to confirm the token in the rule names matches `oap_agent_eval_cluster_info`
- [ ] A spoke reconciler creates and updates no evaluator and no LLM connection: editing a catalog prompt on the hub is reflected in the project, and a spoke pass afterwards leaves it unchanged
- [ ] A spoke running a chart version whose catalog has an evaluator the hub does not yet have records an Event on that agent's ConfigMap, skips only that evaluator, and creates the rule on its own next pass once the hub lands it
- [ ] The judge virtual-key Secret is present only in the hub's `agent-evaluation` namespace, and a spoke reconciler runs without it
- [ ] An agent deployed on the hub with `evaluation` on gets `oap-<hub>-` rules and `cluster`-labelled metrics from the hub instance
- [ ] Two hub passes with an unchanged catalog create no new evaluator version; editing one prompt creates exactly one, and every cluster's rules for that evaluator move to it
- [ ] An agent with `judge.modelId` set to a second alias in `judge.aliases` gets a rule on `<evaluator>--<alias>`; an alias not in the list records an Event and creates no rule
- [ ] A spoke reconciler reaches Langfuse at the hub domain with no hub-local DNS, and its rule calls succeed through the platform ALB
- [ ] A sampled trace from the agent gets a score in Langfuse, and the judge call appears in Bifrost under the judge key, not as an un-keyed request
- [ ] An agent with evaluation turned off gets no scores
- [ ] The judge LLM connection to `bifrost.bifrost.svc.cluster.local` saves, and runs, with only the allowlisted host. Any other in-cluster base URL is still refused
- [ ] `oap_agent_eval_scores` and `oap_agent_eval_pass_ratio` for one agent match a hand count of that agent's scores in Langfuse over the same window
- [ ] The metrics Service is scraped into AMP, and the `Agent Evaluation` dashboard appears in AMG under "Agent Platform" with no manual import
- [ ] A second agent turning on evaluation appears in the dashboard's `agent` list with no dashboard change
- [ ] An idle agent shows no pass ratio (not 0%), and stopping the reconciler turns the data-age panel amber and fires the staleness alert
- [ ] The metrics loop stays within the `public-api-metrics` rate limit at the expected agent count and `metrics.intervalSeconds`

---

## 8. Open questions

1. **Judge family.** Decided, and by constraint rather than preference:
   `judge-default` is `us.amazon.nova-pro-v1:0`, a different family from the
   agents' Claude. Langfuse validates an LLM-as-a-judge evaluator by making a
   real model call that demands **structured output**, and Bifrost translates
   that into a Bedrock forced tool call. Measured against Bedrock through
   Bifrost on the hub:

   | Judge candidate | Structured-output call |
   |---|---|
   | `us.amazon.nova-pro-v1:0` | Valid structured JSON |
   | `us.anthropic.claude-haiku-5-5` | Valid structured JSON |
   | `us.anthropic.claude-sonnet-5-5` | Rejected: `tool_choice: type "tool" and "any" are not supported for this model` |
   | `us.anthropic.claude-opus-5-5` | Rejected, same reason |
   | `us.anthropic.claude-fable-5`, `-5-1` | Rejected by the **account**, not the capability: `data retention mode 'default' is not available for this model`. It fails a plain chat call too, so Fable needs Bedrock model access resolved before it can be judged on merit |
   | `us.anthropic.claude-3-7-sonnet-20250219-v1:0` | `404` end of life |

   This is **not** a Claude-family limitation: Haiku 5.5 accepts forced tool
   choice while Sonnet and Opus 5.5 refuse it. A model that refuses makes
   Langfuse pause the evaluator with `EVAL_MODEL_CONFIG_INVALID`.

   Nova Pro is chosen over Haiku 5.5 because it is the stronger of the two
   working options — D3's own argument is that a small model makes a weak judge
   — and because a different family removes self-preference bias outright
   rather than trusting one Claude to grade another. The original idea of "a
   stronger model of the same family" (Opus judging Sonnet) is simply not
   reachable through this path today.

   Revisit when any of these changes: Bifrost stops forcing `tool_choice`
   (`auto` would likely satisfy Sonnet and Opus), those models start accepting
   it on Bedrock, or Fable's data-retention access is enabled on the account.
   The dashboard breaks pass ratios down by judge model, so a swap stays
   measurable.
2. **Langfuse version.** Decided: upgraded to v4. On the previously pinned
   `3.225.11` the evaluator API is under `/api/public/unstable/` and could not
   be used at all — its preflight fails with
   `failed to get config for provider: not found` even for a catalog model such
   as `openai/gpt-4o-mini` on a connection named `openai`, so it was not
   specific to a Bifrost alias or a custom provider name. v4 exposes the same
   surface as the stable `/api/public/v2/evaluators` and
   `/api/public/v2/evaluation-rules`.

   The upgrade is deliberately split from the storage-model cutover: the server
   runs v4 while `LANGFUSE_MIGRATION_V4_WRITE_MODE` stays `legacy`, so every v3
   ingestion and read surface keeps working (including the OTel collector's path
   and this addon's metrics loop). Moving to `events_only` is a separate,
   one-way decision, and it requires this reconciler's metrics loop to move off
   `/api/public/metrics` first.

   Three v4 API differences change D4's mechanics: evaluators are identified by
   a stable **id** rather than by name (names are explicitly not identifiers),
   a new version is published through `POST /evaluators/{id}/versions` rather
   than by re-posting the name, and the variable mapping now lives on the
   evaluator as a default that a rule's `evaluatorAssignments` may inherit. The
   property D4 depends on survives: a rule always uses the latest version of
   each evaluator it is assigned, so a hub prompt change still reaches every
   cluster with no spoke action.
3. **One shared project, or one per team.** Decided: one shared project for every
   cluster. Cluster-scoped tags and per-cluster rule ownership (D4) keep clusters
   apart within it. A project per cluster would need a project, key pair and
   Secrets Manager entry per cluster, and would split dev and prod traces in the
   Langfuse UI. Still open: whether a project per team is wanted, since a project
   is Langfuse's only trace-access boundary.
4. **The Strands root span.** Rules select the root with `parentObservationId` is
   null (D4), so the span name is not needed. A live trace still has to confirm
   that the root observation carries the user input and the final output.
5. **Sensitive data before judging.** Is Bedrock in-account enough, or must PII
   be masked first? Langfuse server-side masking is an Enterprise feature.
6. **Default evaluator set.** Is `goal-completion` plus `helpfulness` the right
   default, or should `tool-use` be on by default for agents that have
   `mcpServers`?
7. **Where quality alerts live.** The dashboard follows the Dark Factory
   `GrafanaDashboard` pattern. Does the platform's grafana-operator version
   support alert rule CRs, or should alerts be AMP rule groups? Both are
   declarative; the choice depends on which the base platform already uses.
