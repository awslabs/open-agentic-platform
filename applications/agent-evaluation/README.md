# agent-evaluation

The reconciler behind the `agent` component's `evaluation` block. It turns each
agent's declaration into Langfuse online LLM-as-a-judge evaluators and evaluation
rules, and reports pass ratios as Prometheus metrics.

Design: [`docs/architecture/agent-evaluation.md`](../../docs/architecture/agent-evaluation.md).
Addon that deploys it: `gitops/addons/charts/agent-evaluation/`.

```
agent --traces--> Langfuse --sampled--> judge (Bifrost) --score--> Langfuse
                     ^                                               |
                     | evaluators + rules                            | aggregates
                     |                                               v
            [this reconciler] <-- ConfigMaps --- agents      /metrics --> AMP --> Grafana
```

## Roles

One image, two roles, selected by `ROLE`.

| | `spoke` | `hub` |
|---|---|---|
| Watch this cluster's agent ConfigMaps | yes | yes |
| Own this cluster's `oap-<owner token>-` rules | yes | yes |
| Export this cluster's score metrics | yes | yes |
| Write the catalog evaluators | **no** | yes |
| Write the judge LLM connection | **no** | yes |

Every cluster that runs agents runs an instance, the hub included, because
ConfigMaps are local to a cluster. But all clusters share one Langfuse project,
so the project-wide objects — the judge prompts and the judge connection — have
exactly one writer. Otherwise, with trunk-based development putting clusters on
different chart versions, the last cluster to reconcile would decide how the
whole fleet is scored.

That guarantee is enforced in the code path, not by configuration:
`LangfuseClient` is constructed with `allow_project_writes=False` on a spoke and
raises `ProjectWriteForbidden` if those methods are ever reached. The shared
Langfuse key pair would permit the write, so the program has to refuse it.

## Loops

| Loop | Where | Every | Does |
|---|---|---|---|
| `project` | hub only | `RECONCILE_INTERVAL_SECONDS` | Publish catalog evaluators and the judge LLM connection |
| `rules` | every instance | `RECONCILE_INTERVAL_SECONDS` | Diff this cluster's agents into evaluation rules |
| `metrics` | every instance | `METRICS_INTERVAL_SECONDS` | Read score aggregates back and publish gauges |

Each pass is a full diff, so a restart loses nothing and an error is recovered by
the next pass rather than by a retry.

### Why the `rules` loop only reads evaluators

An agent can name an evaluator that this cluster's catalog has but the Langfuse
project does not yet — that is what a cluster upgraded ahead of the hub looks
like. The reconciler records a Kubernetes Event on that agent's ConfigMap and
skips the evaluator. It never creates it. The rule then appears on the pass after
the hub publishes it, with no action on the spoke. A visible, self-healing wait
is the point: creating it locally would silently overwrite the hub's prompt for
every other cluster.

### Why evaluators are written only on change

Evaluators have no `PATCH` at the Langfuse pin. A `POST` with an existing name
mints a new version, and every rule referencing that name moves to it — which is
what makes a hub prompt change reach the whole fleet with no spoke action. It
also means an unconditional `POST` would mint a version every 60 seconds, so the
project loop diffs prompt, model and output schema first.

## Ownership and the owner token

Rules are named `oap-<owner token>-<evaluator>-<config hash>`, where the owner
token is the first 8 hex characters of SHA-256 over the cluster name.

It is a hash rather than the cluster name because ownership is tested for
deletion. EKS cluster names may contain hyphens, so a `oap-<cluster>-` prefix is
ambiguous: `oap-dev-` is also a prefix of every rule owned by a cluster named
`dev-2`, and the `dev` reconciler would delete them on finding no local
ConfigMap. A fixed-length, fixed-alphabet token cannot extend another's, so the
comparison is exact. `tests/test_naming.py` covers that case directly.

The trade-off is that the cluster is not legible in the Langfuse UI, so the
reconciler logs `cluster=<name> owner_token=<token>` at startup and publishes the
pair on `oap_agent_eval_cluster_info`.

## Configuration

All from the chart. Credentials are mounted as files, never set as chart values.

| Variable | Default | Purpose |
|---|---|---|
| `ROLE` | `spoke` | `hub` adds the project loop |
| `CLUSTER_NAME` | — | **Required.** Scopes trace tags, rule names and metrics |
| `LANGFUSE_URL` | — | **Required.** In-cluster on the hub, the hub domain from a spoke |
| `BIFROST_URL` | `http://bifrost.bifrost.svc.cluster.local:8080/v1` | Judge gateway |
| `RECONCILE_INTERVAL_SECONDS` | `60` | Rules and project loops |
| `METRICS_INTERVAL_SECONDS` | `300` | Score aggregate loop |
| `METRICS_WINDOWS` | `24h,7d` | Rolling windows reported |
| `METRICS_PORT` | `9090` | `/metrics`, `/healthz`, `/readyz` |
| `MAX_ACTIVE_RULES` | `50` | Langfuse's documented per-project cap |
| `CATALOG_PATH` | `/etc/agent-evaluation/catalog.yaml` | Rendered from chart values |
| `CREDENTIALS_DIR` | `/etc/agent-evaluation/credentials` | Mounted Secrets |
| `LOG_LEVEL` | `INFO` | |

Credentials read from `CREDENTIALS_DIR`:

- `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` — **required**. The seeded
  project's pair, the same one the OTel Collector uses for ingestion; no admin
  key is minted.
- `BIFROST_VIRTUAL_KEY` — optional. A budget and attribution tag for judge
  spend, not an access credential: Bifrost authenticates to Bedrock with its own
  IAM identity. Absent is tolerated because Bifrost runs with
  `is_vk_mandatory: false`; the judge then runs un-budgeted, which is logged as a
  warning and published as `oap_agent_eval_judge_key_present 0`.

## Endpoints

`/metrics`, `/healthz` and `/readyz` on `METRICS_PORT`, served from the pod
network and exposed only through a ClusterIP Service — the chart creates no
hostPort, NodePort or Ingress.

The probes are split deliberately. `/healthz` answers as soon as the process
serves HTTP and never consults Langfuse, so a slow ExternalSecret sync or an IAM
policy still propagating cannot restart the pod in a loop; `/readyz` is the one
that reports the dependency and returns `503 {"status":"initializing"}` until the
first pass completes.

## Metrics

`oap_agent_eval_scores`, `_passed`, `_pass_ratio`, `_score_avg` (labels:
`cluster`, `namespace`, `agent`, `evaluator`, `window`), `_pass_threshold` (no
`window` — the threshold comes from strictness), `_info`, `_rules_active`,
`_errors_total`, `_last_success_timestamp_seconds`, plus `_cluster_info`,
`_judge_key_present`, `_agents_declared` and `_agents_skipped`.

Two behaviours worth knowing when reading the dashboard:

- **An idle agent has no `pass_ratio` at all**, rather than 0. Zero would read as
  "every response failed"; absent reads as "no traffic yet".
- **A throttled metrics pass publishes nothing** and leaves the previous values
  in place, so the dashboard shows stale-but-true numbers while
  `_errors_total{loop="metrics"}` rises and `_last_success_...` stops moving.
  A half-written gauge set would look like a quality collapse.

## Tests

```bash
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest tests/ -q
```

No cluster or Langfuse instance needed: the Kubernetes and Langfuse clients are
faked in `tests/conftest.py`, and the loops' planning logic is pure.

## Build

```bash
podman build -t agent-evaluation:dev .
```
