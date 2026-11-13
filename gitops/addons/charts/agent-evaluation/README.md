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

`alerts.enabled` ships a PrometheusRule, rendered only when the cluster has the
prometheus-operator CRDs (`monitoring.coreos.com/v1`); elsewhere it is skipped so
the rest of the chart still syncs. These alert on the **pipeline**, not on agent
quality: a falling pass ratio is a product signal for the owning team to
read on the dashboard, and a deliberately strict evaluator on a hard task is
supposed to score low. What the platform owns is whether the numbers are current
and real.

| Alert | Fires when |
|---|---|
| `AgentEvaluationDataStale` | No full metrics pass for `alerts.dataAgeSeconds` (per cluster, so one stalled cluster is not hidden) |
| `AgentEvaluationReconcileErrors` | Langfuse calls failing for 30m in a loop other than `harness` |
| `AgentEvaluationRuleBudgetHigh` | The shared project is at 80% of `maxActiveRules` |
| `AgentEvaluationJudgeKeyMissing` | Hub only: judge spend is un-budgeted |
| `AgentHarnessStale` | Only with `harness.enabled`: no harness pass for `max(3 x harness.intervalSeconds, 900s)`, for 15m; also follows a missing required judge key |
| `AgentHarnessReconcileErrors` | Only with `harness.enabled`: the harness loop's Kubernetes (Job, Rollout, Service) or Bifrost management API calls failing for 30m |
| `AgentHarnessJudgeKeyMissing` | Only with `harness.enabled`: the harness judge key is missing for 30m; `warning` with `required: true` (no run starts while it fires), `info` with `required: false` (runs go on un-budgeted) |

## Harness (report mode)

The pre-promotion harness from issue #137, off by default. With
`harness.enabled: true` the reconciler runs an agent's `harness` dataset against
every new revision's `<agent>-preview` Service and publishes
`oap_agent_harness_*`. It **never blocks or delays a promotion**. Design and
contracts: [§4b](../../../../docs/architecture/agent-evaluation.md#4b-the-agent-harness-phase-3-report-mode).

Nothing in the table below renders while `harness.enabled` is false, not even a
`HARNESS_ENABLED` env var (the reconciler defaults it to false). The one
harness part of a disabled render is the dashboard's "Harness" row (see below).

Turning the harness off after it ran: the reconciler makes the catalog
ConfigMap the owner of every harness Job and of the judge-key Secret (each
result ConfigMap is owned by its Job), so once Argo CD prunes the catalog the
garbage collector removes them. The minted Bifrost virtual key stays in
Bifrost; delete it by hand as described in `agent-harness.md` ("Turning the
harness off").

| Renders | What |
|---|---|
| ConfigMap `agent-evaluation-harness-catalog` | `checks.yaml`: `harness.checks` with each prompt from `files/harness/checks/`. Fails the render on a missing prompt or a `casePassScore` outside [0, 1] |
| ConfigMap `agent-evaluation-harness-datasets` | every `files/harness/datasets/*.yaml`. Fails the render when there is none |
| Deployment | the `HARNESS_*` env, both ConfigMaps mounted read-only, and a `checksum/harness` annotation so a dataset or prompt change restarts the reconciler |
| RBAC | see below |
| PrometheusRule | `AgentHarnessStale`, `AgentHarnessReconcileErrors`, `AgentHarnessJudgeKeyMissing` (where the CRD exists) |

The Grafana dashboard is one static JSON, so it always contains the "Harness"
row, harness on or off. Its panels show "No data" for agents with no harness and
on clusters with the harness off.

| Value | Default | Meaning |
|---|---|---|
| `harness.intervalSeconds` | `60` | Harness loop interval |
| `harness.maxConcurrentJobs` | `2` | Harness Jobs at once on this cluster |
| `harness.historyLimit` | `3` | Finished runs kept per agent |
| `harness.caseConcurrency` | `2` | Cases in flight within one run |
| `harness.caseTimeoutSeconds` | `120` | Per call (agent call, each judge call) |
| `harness.readyTimeoutSeconds` | `600` | Wait for the preview's agent card |
| `harness.runTimeoutSeconds` | `1800` | Whole run; the runner stops 60 s before it with a partial result |
| `harness.maxCases` | `50` | Larger datasets are a run error |
| `harness.judge.alias` | `""` | Judge alias; empty is `judge.defaultAlias` |
| `harness.judge.virtualKey.*` | see `values.yaml` | The harness judge key: `mint`, `required`, `secretRef`, `bifrostAdminUrl`, `allowedModels`, `budget`, `rateLimit` |
| `harness.checks` | `expected-answer` (0.7) | Check prompt file and per-case pass score |
| `harness.runner.resources` | 50m / 128Mi, 256Mi limit | The Job container |

**RBAC.** The ClusterRole gains read-only `get`/`list`/`watch` on `argoproj.io`
`rollouts` and `get` on `services` (to check the `<agent>-preview` Service
before a run). Everything else is a Role in the release namespace: the reconciler
creates and deletes the harness Jobs, deletes their result ConfigMaps, creates
Secrets and gets/updates only `harness.judge.virtualKey.secretRef`. The Jobs run
as a separate ServiceAccount, `agent-evaluation-harness-runner`, which can only
create ConfigMaps in this namespace. Nothing new is granted in any agent
namespace.

**Judge key.** A harness-only Bifrost virtual key, separate from the ONLINE judge
key in `judge.virtualKey` (which stays hub-only). The reconciler mints it in
**this** cluster's Bifrost through the management API, because a virtual key is
only valid in the Bifrost that minted it, and writes it to the
`secretRef` Secret, which the Jobs mount. There is no Secrets Manager entry and
nothing to do on the hub. With `required: true` (the default) no run starts
without it. Minting needs Bifrost governance auth to be off in-cluster, as it is
today; when that changes, set `mint: false` and provide the Secret another way.

**Datasets.** `files/harness/datasets/<name>.yaml`, referenced by name from an
agent's `harness.dataset`. Two ship: `general-assistant-v1` (20 cases for a
general assistant without tools) and `example-smoke-v1` (the four-case format
example). Neither was harvested from real failures and neither is a golden set.

**Enable it** per cluster through the overlay values file the registry entry
already reads, `gitops/overlays/clusters/<cluster>/agent-evaluation/values.yaml`:

```yaml
harness:
  enabled: true
```

The image must be one that contains the harness (`0.3.0` or later).

## Rule budget

Langfuse caps active evaluation rules per project and the whole fleet shares one
project, so rules are grouped by `(evaluator, judge, sampling)` per cluster
rather than created per agent. Teams using the three `coverage` levels group into
far fewer rules than arbitrary `sampling` numbers, which is why `sampling` is
documented as an escape hatch. Past the cap the reconciler reports the limit
rather than letting Langfuse 409 part-way through a pass.
