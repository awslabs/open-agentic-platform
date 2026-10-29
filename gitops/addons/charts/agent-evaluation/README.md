# agent-evaluation addon

Continuous evaluation of agents from the traces they already emit. Deploys the
reconciler, the platform's evaluator catalog, the judge LLM connection, the
metrics Service, the Grafana dashboard and the pipeline alerts.

- Design: [`docs/architecture/agent-evaluation.md`](../../../../docs/architecture/agent-evaluation.md)
- Reconciler source: [`applications/agent-evaluation/`](../../../../applications/agent-evaluation/)
- Turn it on for an agent: [`platform/oam/examples/example-agent-evaluation.yaml`](../../../../platform/oam/examples/example-agent-evaluation.yaml)

## Deployment

Enabled per cluster with `agent_evaluation: true` in the environment overlay,
which produces the `enable_agent_evaluation` cluster-secret label the registry
entry selects on (`gitops/addons/registry/observability.yaml`, sync-wave 7 —
after langfuse and otel-collector, because the reconciler needs the seeded
project's keys in Secrets Manager).

It belongs on **every cluster that runs agents, the hub included**: agents' 
evaluation ConfigMaps are local to a cluster, and one reconciler cannot list
another's.

`role` is not set by hand. The registry entry derives it the same way the
otel-collector derives its Langfuse endpoint: the hub is the cluster with a
non-empty `ingress_domain_name`, a spoke has none. That also picks `langfuse.url`
— in-cluster on the hub, the hub domain over HTTPS from a spoke.

## What each role owns

Both roles reconcile their own cluster's evaluation rules and export their own
agents' metrics. The **hub** additionally owns the two project-wide objects: the
catalog evaluators and the judge LLM connection. Every cluster shares one
Langfuse project, and under trunk-based development clusters sit on different
chart versions, so a single writer is what stops an older cluster rolling a judge
prompt back for the whole fleet.

## Required values

The chart fails to render rather than install something that misbehaves:

| Value | Why it has no default |
|---|---|
| `global.clusterName` | Scopes rule names, trace tags and metrics. Without it two clusters would write each other's objects in the shared project |
| `langfuse.url` | Differs between hub and spoke and cannot be guessed |
| `langfuse.secretManagerKey` | Without it the reconciler has no credential for the Langfuse API |

## Credentials

Both come from Secrets Manager through ExternalSecret, into this namespace only.
**Never put either value in a chart value or any other file in git.**

- **Langfuse API key pair** — required. Read from the entry the langfuse addon
  writes (`<resourcePrefix>-hub/langfuse-otel`), the same pair the OTel Collector
  uses for ingestion, so no admin key is minted.
- **Bifrost judge virtual key** — optional, hub only, `judge.virtualKey.enabled`.
  It is a budget and attribution tag rather than an access credential: Bifrost
  authenticates to Bedrock with its own IAM identity. Off by default because an
  ExternalSecret pointing at a non-existent entry stays permanently NotReady,
  which is a worse first run than a warning; when absent the judge works but its
  spend is not attributed to a budget, and `AgentEvaluationJudgeKeyMissing`
  fires on the hub after an hour.

## The catalog

`catalog` in values plus a prompt file per entry under `files/evaluators/`.
Changing a prompt is a reviewed chart change — which is the point of the catalog
being platform-owned. The Deployment carries a checksum of the catalog and the
prompt files, so an edit restarts the reconciler and the hub republishes it.

Four entries ship by default: `goal-completion`, `helpfulness`, `safety` and
`tool-use`. `passThreshold` per strictness level is used only by the reports; it
is applied when pass ratios are computed, not by the Langfuse rule, so strictness
levels cost no extra rules.

## Judge aliases

`judge.aliases` lists every Bifrost alias an agent may name in
`evaluation.judge`. At the Langfuse pin the judge model lives on the *evaluator*
and an evaluation rule has no model field, so the hub writes one evaluator per
catalog entry per alias (`<evaluator>--<alias>`); the default judge takes the
bare name. Adding an alias therefore multiplies evaluator objects, not rules.

A judge an agent names that is not in this list is reported as a Kubernetes Event
on that agent's ConfigMap and no rule is created — deliberately not a silent
fallback to the default, since that would score the agent with a judge the team
did not ask for.

## Alerts

`alerts.enabled` ships a PrometheusRule. These alert on the **pipeline**, not on
agent quality: a falling pass ratio is a product signal for the owning team to
read on the dashboard, and a deliberately strict evaluator on a hard task is
supposed to score low. What the platform owns is whether the numbers are current
and real.

| Alert | Fires when |
|---|---|
| `AgentEvaluationDataStale` | No full metrics pass for `alerts.dataAgeSeconds` (per cluster, so one stalled cluster is not hidden) |
| `AgentEvaluationReconcileErrors` | Langfuse calls failing for 30m |
| `AgentEvaluationRuleBudgetHigh` | The shared project is at 80% of `maxActiveRules` |
| `AgentEvaluationJudgeKeyMissing` | Hub only: judge spend is un-budgeted |

## Rule budget

Langfuse caps active evaluation rules per project and the whole fleet shares one
project, so rules are grouped by `(evaluator, judge, sampling)` per cluster
rather than created per agent. Teams using the three `coverage` levels group into
far fewer rules than arbitrary `sampling` numbers, which is why `sampling` is
documented as an escape hatch. Past the cap the reconciler reports the limit
rather than letting Langfuse 409 part-way through a pass.
