# Continuous Agent Evaluation: LLM-as-a-Judge on Agent Observability Traces

| | |
|---|---|
| **Status** | Proposed |
| **Author** | @elamaran11 |
| **Reviewers** | TBD |
| **Target** | OAP agent platform: `agent` component, new `agent-evaluation` addon |
| **Date** | 2026-10-22 |
| **Revision** | 1 |
| **Issue** | #79 |

## Scope

**In scope**

- Online evaluation of deployed agents from the traces they already emit
- A declarative `evaluation` block on the `agent` component, so evaluation is
  configured in the agent's OAM Application and not wired by hand in a UI
- A judge model chosen by the platform, overridable per agent
- Per-agent quality reports over a rolling window, for example *"91% of 1,000
  sampled interactions passed `goal-completion`"*

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
          sampling: 0.1
          evaluators: [goal-completion, helpfulness, tool-use]
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
| `centralized` (default) | Agent → Langfuse (Strands telemetry, direct OTLP) | `platform/oam/definitions/components/agent.cue` (`LANGFUSE_*` env) |
| `decentralized` | Agent → ADOT → CloudWatch GenAI Observability | same file (`OTEL_PYTHON_DISTRO=aws_distro`) |

Every trace carries the agent's name as a Langfuse tag
(`applications/strands-agent-base/app/agent.py`, `_trace_attributes`:
`langfuse.trace.tags = [source:<chat|rca>, <AGENT_NAME>]`), plus `session.id` and
`user.id`. So a trace can already be attributed to a specific agent, without any
change to the agent.

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

**Where the others fit.**

- **C is the backend for `observability.mode: decentralized`.** AgentCore
  Evaluations does not require AgentCore Runtime: its prerequisites accept "an
  agent built with a supported framework configured with AgentCore Observability,
  including Transaction Search". Strands Agents is a supported framework. That is
  the `decentralized` path, so the same `evaluation` block maps to an
  `OnlineEvaluationConfig` for those agents (Phase 2).
- **RAGAS fits offline evaluation**: a Job that runs a dataset through an agent
  before promotion and scores it with reference-based metrics (Phase 3).

### D2. The abstraction: an `evaluation` block on `agent`, backed by a platform catalog

The application developer chooses **which** evaluators run, **how often**, and
optionally **which judge**. The platform engineer owns **what each evaluator
means**: its prompt, scoring scale and variable mapping, in the addon's catalog.
That keeps judge prompts reviewed and versioned in one place, the same way
`mcpServers` names a server without restating how to reach it.

File: `platform/oam/definitions/components/agent.cue` (proposed `parameter` addition)

```cue
// +usage=Online evaluation of this agent's live traces by an LLM judge.
// Requires the agent-evaluation addon on the cluster.
evaluation?: {
	// +usage=Turn evaluation on for this agent.
	enabled: *false | bool
	// +usage=Judge model, as a Bifrost model alias. Defaults to the platform judge.
	judge?: modelId: string
	// +usage=Fraction of traces scored, 0-1. Each sampled trace costs one judge call per evaluator.
	sampling: *0.1 | number & >0 & <=1
	// +usage=Evaluators from the platform catalog.
	evaluators: *["goal-completion", "helpfulness"] | [...("goal-completion" | "helpfulness" | "tool-use" | "safety" | "topic-adherence")]
}
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
			sampling:   "\(parameter.evaluation.sampling)"
			evaluators: strings.Join(parameter.evaluation.evaluators, ",")
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
| **Platform default judge, overridable per agent** (**chosen**) | The platform engineer sets one Bifrost alias, for example `judge-default`, to a more capable model. An application developer can override it with `evaluation.judge.modelId`. |
| The developer must always name a judge | Rejected. Most teams have no basis for choosing one, so it should be a platform default. |

Rules:

- **The judge is reached through Bifrost.** Langfuse LLM connections accept any
  OpenAI-compatible base URL, and Bifrost serves one at `/v1`. The addon creates
  one Langfuse LLM connection that points at Bifrost and uses a judge-only virtual
  key. Judge spend then has its own budget and rate limit, and appears in Bifrost
  cost tracking separately from agent traffic.
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

File: `gitops/addons/configs/bifrost/values.yaml` (proposed alias addition)

```yaml
aliases:
  claude-sonnet: us.anthropic.claude-sonnet-5-5
  # Platform default evaluation judge; agent-evaluation addon. Q1 decides the model.
  judge-default: <judge inference profile>
```

### D4. Reconciliation: one platform reconciler, not a Job per agent

Component CUE cannot call an API at render time, so something has to turn the
declaration into Langfuse evaluators and rules.

| Option | Verdict |
|---|---|
| **One reconciler Deployment in the `agent-evaluation` namespace** (**chosen**) | It watches ConfigMaps labelled `evaluation.oap.io/agent`, and holds the Langfuse key and the judge virtual key itself. Because it is level-triggered, deletion is a diff. |
| A Job per agent, emitted by the component | Rejected. It puts Langfuse admin credentials in every agent namespace, does not re-run on drift, and leaves rules behind when the agent is deleted. The per-agent virtual-key mint Job in #18 hit the same three problems. |
| A CRD and an operator | A heavier version of the chosen option. Worth it later if the declaration grows. |

The reconciler loop, each interval:

1. List every `evaluation.oap.io/agent` ConfigMap across namespaces.
2. Ensure each catalog evaluator exists in Langfuse
   (`POST /api/public/unstable/evaluators`, versioned by name). The prompts come
   from the addon's catalog.
3. **Group agents by `(evaluator, judge, sampling)`**, and keep one evaluation
   rule per group, filtered to the trace tags of the agents in that group. Create
   new rules and `PATCH` changed ones, so the operation is idempotent.
4. Delete rules whose group is now empty.

**Why grouping.** Langfuse allows **at most 50 effectively active evaluation
rules per project**. The platform seeds one shared project
(`seed.projectId: proj-agent-platform` in `gitops/addons/charts/langfuse/values.yaml`).
One rule per agent per evaluator would hit that limit at about 16 agents with
three evaluators. With grouping, the number of rules depends on the number of
distinct evaluator, judge and sampling combinations, not on the number of agents.

File: `gitops/addons/charts/agent-evaluation/values.yaml` (proposed)

```yaml
langfuse:
  url: http://langfuse.langfuse.svc.cluster.local:3000
  # ExternalSecret from <resourcePrefix>-hub/langfuse-otel, the Secrets Manager
  # entry the langfuse addon already writes (templates/push-otel-secret.yaml).
  apiKeySecretRef: langfuse-api-key
judge:
  defaultModelId: judge-default
  bifrostUrl: http://bifrost.bifrost.svc.cluster.local:8080/v1
  # Judge-only Bifrost virtual key with its own budget.
  virtualKeySecretRef: bifrost-judge-vk
reconcile:
  intervalSeconds: 60
catalog:
  goal-completion:
    target: root-agent-observation
    scale: boolean
    prompt: files/evaluators/goal-completion.md
  helpfulness:
    target: root-agent-observation
    scale: 0-1
    prompt: files/evaluators/helpfulness.md
  tool-use:
    target: root-agent-observation
    scale: boolean
    prompt: files/evaluators/tool-use.md
```

**Target the root agent observation.** Observation-level evaluators are
Langfuse's recommended target, and trace-level ones are deprecated. An
observation evaluator does not read its child observations, though. So rules
target the agent's root invocation span, which carries the user input and final
output, and `tool-use` maps the tool-call field of that observation. The exact
Strands span name still has to be confirmed (Q4).

### D5. Reports: Langfuse score analytics first, with a metric for gates

- **Per-agent report**: scores are Langfuse scores, filterable by the agent tag.
  The pass rate over a window (for example *"91% of the last 1,000 sampled
  traces"*) is a Langfuse dashboard widget over the score.
- **A metric for automation**: the reconciler also reads aggregates through the
  Langfuse metrics API and serves them on `/metrics`, for example
  `oap_agent_eval_pass_ratio{agent,evaluator}`. Alarms and an Argo Rollouts
  analysis step can then use it (Phase 3).
- **No backfill.** Evaluation rules apply to traces that arrive after the rule is
  created. Turning on evaluation starts scoring from that moment, which the
  `+usage` text says.

---

## 4. Architecture

```mermaid
flowchart LR
  subgraph NS["agent namespace (application developer)"]
    A["agent Rollout"] -- "OTLP traces<br/>tag = agent name" --> LF
    CM["ConfigMap &lt;agent&gt;-evaluation<br/>(from evaluation: block)"]
  end
  subgraph EV["agent-evaluation namespace (platform)"]
    R["evaluation reconciler"]
  end
  subgraph LFN["langfuse namespace"]
    LF["Langfuse web + worker"]
  end
  BF["Bifrost /v1<br/>judge virtual key"]
  CM -- "watch" --> R
  R -- "evaluators + rules API" --> LF
  LF -- "sampled observations<br/>LLM connection" --> BF
  BF --> BR["Amazon Bedrock<br/>judge-default"]
  LF -- "scores on trace" --> LF
  R -- "metrics API" --> LF
  R -- "/metrics pass ratio" --> M["Prometheus / AMP"]
```

| Stage | Actor |
|---|---|
| Adds `evaluation:` to the agent's Application | Application developer |
| Defines the evaluator catalog, the judge alias and the judge budget | Platform engineer |
| Renders the declaration ConfigMap | KubeVela |
| Creates and updates the Langfuse evaluators and rules | Evaluation reconciler |
| Samples traces and calls the judge | Langfuse worker |
| Reads reports and alarms | Application developer, platform engineer |

---

## 5. Security and cost

| Concern | Control |
|---|---|
| Langfuse credentials | Held only by the reconciler in `agent-evaluation`, never in an agent namespace |
| Judge model access | One judge virtual key in Bifrost, with a budget and a rate limit. The judge never uses an agent's key |
| Trace content sent to a model | The judge runs through Bifrost to Bedrock in the same account. Sensitive-data masking before judging is Q5 |
| Cost | Cost per agent is about traces × `sampling` × evaluators judge calls. The default `sampling: 0.1` with two evaluators is 0.2 judge calls per trace. The Bifrost budget caps the total |
| A developer weakening evaluation | The developer controls only `enabled`, `sampling`, the `judge` alias and evaluator names. Prompts and scales are in the platform catalog |

---

## 6. Phases

| Phase | Delivers |
|---|---|
| **1** | `agent-evaluation` addon (reconciler, catalog, judge LLM connection), `evaluation` block for `centralized` agents, Langfuse dashboard, example and live check |
| **2** | `decentralized` agents: the reconciler maps the same declaration to an AgentCore `OnlineEvaluationConfig`, with the catalog entries mapped to built-in or custom evaluators |
| **3** | Offline evaluation gate: a dataset Job (Langfuse experiment target, or RAGAS reference-based metrics) run against the blue-green preview before promotion, read by an Argo Rollouts analysis step |

---

## 7. Validation plan

- [ ] CUE renders with `evaluation` absent, with it off, and with it on, using non-default `judge` and `sampling` values
- [ ] Turning on evaluation for two agents with the same settings produces **one** rule per evaluator, filtered to both tags
- [ ] Deleting one agent's Application removes its tag from the rule, and deleting the last agent removes the rule
- [ ] A sampled trace from the agent gets a score in Langfuse, and the judge call appears in Bifrost under the judge key
- [ ] An agent with evaluation turned off gets no scores
- [ ] The pass-ratio metric matches the Langfuse dashboard over the same window

---

## 8. Open questions

1. **Judge family.** Should `judge-default` be a stronger model in the same family
   (for example Opus judging a Sonnet agent, as in the issue), or a different
   family to remove self-preference entirely?
2. **Langfuse version.** At the pinned `3.225.11` the evaluator APIs are under
   `/api/public/unstable/`. They became stable on Langfuse `main` on 2026-08-27,
   and releases are now on `v4.x`. Should the platform upgrade Langfuse before
   Phase 1, or ship on the unstable endpoints with a version pin?
3. **One shared project or a project per team.** Grouping keeps one shared
   project under the 50-rule limit, but a project per team also separates trace
   access. Is that wanted?
4. **The Strands root span name** that rules should target. It has to be confirmed
   against a live trace.
5. **Sensitive data before judging.** Is Bedrock in-account enough, or must PII
   be masked first? Langfuse server-side masking is an Enterprise feature.
6. **Default evaluator set.** Is `goal-completion` plus `helpfulness` the right
   default, or should `tool-use` be on by default for agents that have
   `mcpServers`?
