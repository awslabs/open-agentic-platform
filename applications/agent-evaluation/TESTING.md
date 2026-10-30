# Testing continuous agent evaluation

How to prove the whole chain works, end to end:

```
OAM Application ──> <agent>-evaluation ConfigMap ──> reconciler ──> Langfuse evaluation rule
                                                                            │
agent ──OTLP──> OTel Collector ──> Langfuse trace ───────────────────────────┘
                                          │
                                          └──> judge (Bifrost → Nova Pro) ──> score on the trace
                                                                                    │
                                   reconciler metrics loop ──> /metrics ──> AMP ──> Grafana
```

Everything below was run on the `hub` cluster and passed. If you are re-running
it after a change, work top to bottom: each step's failure mode is distinct, and
checking them in order tells you which link broke.

## Start here — the hub is already set up

Nothing needs installing or applying. Langfuse v4 is deployed in `dual` write
mode, the reconciler is running, the four catalog evaluators are published, and
**both** smoke-test agents (`eval-smoketest`, `eval-imageless`) are deployed and
have already been scored.

So for a confirmation run, go straight to **§4 → §5 → §6 → §7**: send a request,
watch the trace arrive, watch the score land, see the pass ratio. Roughly two
minutes.

§0–§3 are the diagnostic path — read them when something does *not* work, or
when setting this up on another cluster. Any `kubectl apply` in this document is
for a fresh cluster or a re-run after teardown; on the hub today those resources
already exist and applying them again is a no-op.

If what you want is not "does the chain work" but **"show me every object"** —
the reconciler, both ConfigMaps, the credentials, the agents, the Langfuse
evaluators and rules, the metrics endpoint — go to
[Appendix A](#appendix-a--inspect-every-object-one-command-at-a-time). It is a
flat, read-only inventory: every command is a `get`, a `describe` or a `GET`,
in dependency order, with the expected shape of each answer.

Two environment variables are used throughout:

```bash
kubectl port-forward -n langfuse svc/langfuse 3999:3000 &
export LF=http://127.0.0.1:3999
export PK=$(aws secretsmanager get-secret-value --secret-id peeks-hub/langfuse-otel \
  --query SecretString --output text | jq -r .LANGFUSE_PUBLIC_KEY)
export SK=$(aws secretsmanager get-secret-value --secret-id peeks-hub/langfuse-otel \
  --query SecretString --output text | jq -r .LANGFUSE_SECRET_KEY)
```

---

## 0. Prerequisites

| Thing | Expected | Check |
|---|---|---|
| Langfuse | **v4**, write mode `dual` | `curl -s <lf>/api/public/health` → `"version":"4.x"`; `kubectl get cm langfuse-config -n langfuse -o jsonpath='{.data.LANGFUSE_MIGRATION_V4_WRITE_MODE}'` → `dual` |
| ClickHouse | ≥ 25.12 | `kubectl exec -n langfuse langfuse-clickhouse-0 -- clickhouse-client --query "SELECT version()"` |
| Reconciler | `1/1 Running`, ready | `kubectl get pods -n agent-evaluation` |
| Judge alias | resolves | see step 2 |

`dual` write mode is not optional. In `legacy` mode Langfuse never creates an
evaluation job, so a perfectly correct rule scores nothing and the only symptom
is silence.

---

## 1. The reconciler is healthy and owns its objects

```bash
kubectl port-forward -n agent-evaluation svc/agent-evaluation-metrics 9098:9090 &
curl -s http://127.0.0.1:9098/readyz | jq
```

Expect `{"status":"ready","role":"hub","cluster":"hub","evaluators":4,...}`.

On a **hub**, `ready` requires BOTH loops to have succeeded — the rules loop and
the project loop that owns the catalog evaluators. If it says
`"waitingOn":{"project": ...}` the hub cannot publish evaluators, and nothing
downstream will work. `/healthz` stays 200 regardless, by design, so a bad
dependency never becomes a restart loop.

---

## 2. The judge actually answers a structured-output call

This is the single most common failure, and it fails silently: Langfuse accepts
the evaluator (HTTP 201) and then **pauses** it.

```bash
kubectl run jt --rm -i --restart=Never -n bifrost --image=curlimages/curl:8.11.1 --command -- \
  curl -s -m 90 -X POST http://bifrost.bifrost.svc.cluster.local:8080/v1/chat/completions \
  -H 'Content-Type: application/json' -d '{
    "model":"us.amazon.nova-pro-v1:0",
    "messages":[{"role":"user","content":"Score this: hello"}],
    "max_tokens":200,
    "response_format":{"type":"json_schema","json_schema":{"name":"score","strict":true,
      "schema":{"type":"object","properties":{"score":{"type":"number"},"reasoning":{"type":"string"}},
      "required":["score","reasoning"],"additionalProperties":false}}}}'
```

Expect JSON content like `{"score":0,"reasoning":"..."}`.

A `tool_choice: type "tool" and "any" are not supported for this model` error
means that model cannot be a judge here: Langfuse validates a judge by making a
real structured-output call, which Bifrost turns into a Bedrock **forced tool
call**. Measured on this cluster:

| Model | Works as judge |
|---|---|
| `us.amazon.nova-pro-v1:0` | yes (default, `judge-default`) |
| `us.anthropic.claude-haiku-5-5` | yes (`judge-haiku`) |
| `us.anthropic.claude-sonnet-5-5` | no — rejects forced tool choice |
| `us.anthropic.claude-opus-5-5` | no — same |
| `us.anthropic.claude-fable-5(-1)` | no — account-level: `data retention mode 'default' is not available for this model` |

Then confirm no evaluator is paused:

```bash
curl -s -u "$PK:$SK" "$LF/api/public/v2/evaluators?limit=20" \
  | jq -r '.data[] | "\(.name) v\(.version) \(.status) \(.modelConfig.model)"'
```

Every row must say `active`. A `paused` row with `EVAL_MODEL_CONFIG_INVALID`
means the judge model failed the call above. The reconciler also logs this as an
error and republishes the definition to clear it.

---

## 3. Turn evaluation on for an agent — both examples

> **Both are ALREADY DEPLOYED on the hub.** Nothing to apply — skip to the
> verification below, then go to §4. The `kubectl apply` commands are only for
> recreating them on a fresh cluster, or after §"Tearing the smoke tests down".

Two smoke tests exist, and **both should be tested**. They differ in one line
and exercise the same chain:

| Example | `image:` | Proves |
|---|---|---|
| `example-agent-evaluation-smoketest.yaml` | pinned | evaluation works when an Application names its own image |
| `example-agent-evaluation-imageless.yaml` | **absent** | evaluation works in the shape an ordinary Application has |

Confirm what is running:

```bash
kubectl get applications.core.oam.dev -n agents
# eval-smoketest   eval-smoketest   agent   running   true
# eval-imageless   eval-imageless   agent   running   true

# the declaration the component rendered for each
kubectl get configmap -n agents -l evaluation.oap.io/agent
kubectl get configmap eval-smoketest-evaluation eval-imageless-evaluation -n agents \
  -o jsonpath='{range .items[*]}{.metadata.name}{" evaluators="}{.data.evaluators}{" sampling="}{.data.sampling}{"\n"}{end}'
```

A third agent, `my-agent`, also runs in that namespace with **no** `evaluation`
block — no ConfigMap, no tag in the rule, never judged. That is the opt-in
behaviour working, not a fault.

Only if they are absent (fresh cluster, or after teardown):

```bash
kubectl apply -f platform/oam/examples/example-agent-evaluation-smoketest.yaml
kubectl apply -f platform/oam/examples/example-agent-evaluation-imageless.yaml
```

Both use `sampling: 1` and a single evaluator so a score appears in about a
minute. Both are wrong for production — judging every trace costs a judge call
per request, which is what `coverage` exists to control.

### Why imageless is the one that can silently fail

The scoped `agent:<cluster>/<namespace>/<name>` trace tag that evaluation rules
filter on is emitted by the **image**, while the rule is created by the
**platform**. The two can drift: an image predating the tag is configured for
evaluation and never scored, and nothing errors — the rule is valid, the agent
is healthy, there are simply no scores.

Imageless uses the platform default, `global.agentImage` on the
oam-agent-components chart, so that default has to be an image carrying the tag.
Check which image an imageless agent actually resolved to:

```bash
kubectl get pods -n agents -o yaml | grep -E '^\s+image: .*strands-agent' | sort -u
kubectl get componentdefinition agent -n vela-system -o yaml | grep -oE 'image: \*\\"[^\\]*'
```

Measured on this cluster: the published `public.ecr.aws/z0a4o2j5/strands-agent:v1.4.10`
emits only `['<agent>', 'source:chat']` — **no scoped tag** — so imageless does
not score against it. The hub's `global.agentImage` is therefore pointed at
`940019131157.dkr.ecr.us-west-2.amazonaws.com/strands-agent:v1.3.4-tracetag`,
built from this branch. Once the tag ships in the public image, set
`global.agentImage` back to it and the override disappears.

> Current hub state: `oam-agent-components-hub` has auto-sync **paused** and the
> `agent` ComponentDefinition is hand-applied with that image, because the app
> otherwise syncs from `feat/agent-long-term-memory` and reverts it. Restore
> with:
> ```bash
> kubectl patch applicationset oam-agent-components -n argocd --type merge \
>   -p '{"spec":{"template":{"spec":{"syncPolicy":{"automated":{"allowEmpty":true,"prune":true,"selfHeal":true}}}}}}'
> ```
> Auto-sync was paused rather than repointing that app, so the
> `agentcore-memory` component on `feat/agent-long-term-memory` is untouched.

### The rule the reconciler built

Check the rule itself, not the logs. The rule already exists, so
`grep "created rule"` finds nothing — the reconciler is level-triggered and logs
only when it changes something. Silence here means "already correct".

```bash
curl -s -u "$PK:$SK" "$LF/api/public/v2/evaluation-rules?limit=10" | jq '.data[] | {name, enabled, sampling, filter}'
```

Expect **one** rule covering **both** agents:

```
name:     oap-08d33503-helpfulness-8c49ba7c
enabled:  true
sampling: 1
filter:   type any of [AGENT]
          tags any of ["agent:hub/agents/eval-imageless","agent:hub/agents/eval-smoketest"]
```

One rule for two agents is correct, not a bug: rules are grouped by
`(evaluator, judge, sampling)`, and both agents asked for the same
`(helpfulness, judge-default, 1)`, so they share one rule and appear as two tags
in its filter. Give one of them a different `sampling` or `judge` and you get a
second rule.

Note also that the catalog publishes **four** evaluators
(`goal-completion`, `helpfulness`, `safety`, `tool-use`) while only one rule
exists — evaluators are reusable definitions, rules are usage. Three are
published and unused.

`oap-08d33503-…` — the second segment is this cluster's owner token, not its
name. Map it with:

```bash
curl -s http://127.0.0.1:9098/metrics | grep oap_agent_eval_cluster_info
```

If you *did* just recreate the Applications, then the logs are worth watching:

```bash
kubectl logs -n agent-evaluation deploy/agent-evaluation-reconciler --tail=50 | grep -E "created rule|updated rule"
# created rule oap-08d33503-helpfulness-8c49ba7c (evaluator=helpfulness judge=judge-default sampling=1.0 agents=1)
```

---

## 4. Drive traffic — over A2A, not /chat

**Use the A2A endpoint.** `/chat` answers correctly but is not the traced path,
so it produces no trace and nothing to score. This cost an hour the first time.

Pinned-image agent:

```bash
kubectl port-forward -n agents svc/eval-smoketest-stable 8083:8083 &
curl -s -X POST http://127.0.0.1:8083/ -H 'Content-Type: application/json' -d '{
  "jsonrpc":"2.0","id":"1","method":"message/send",
  "params":{"message":{"role":"user","kind":"message","messageId":"m1",
    "parts":[{"kind":"text","text":"In one sentence, what is Kubernetes RBAC for?"}]}}}'
```

Imageless agent (note the different local port, so both can run at once):

```bash
kubectl port-forward -n agents svc/eval-imageless-stable 8084:8083 &
curl -s -X POST http://127.0.0.1:8084/ -H 'Content-Type: application/json' -d '{
  "jsonrpc":"2.0","id":"2","method":"message/send",
  "params":{"message":{"role":"user","kind":"message","messageId":"m2",
    "parts":[{"kind":"text","text":"One sentence: what is a Kubernetes namespace?"}]}}}'
```

---

## 5. The trace carries the cluster-scoped tag

```bash
curl -s -u "$PK:$SK" "$LF/api/public/traces?limit=3" | jq -r '.data[] | "\(.timestamp) \(.tags)"'
```

Expect the scoped tag on **both** agents:

```
["agent:hub/agents/eval-smoketest","eval-smoketest","source:chat"]
["agent:hub/agents/eval-imageless","eval-imageless","source:chat"]
```

If the pinned one has it and the imageless one does not, the platform default
image is the problem, not the platform — see §3.

The scoped `agent:<cluster>/<namespace>/<name>` tag is what the rule filters on,
and it is what keeps the same Application on dev and prod from sharing one
series. If it is missing, the agent image predates it — the platform default
image does; the smoke test pins one that has it.

No trace at all means the collector cannot reach Langfuse. On the hub it must
export **in-cluster**; exporting via the public domain hangs and silently drops
every trace:

```bash
kubectl get cm -n otel -o yaml | grep -o 'https\?://[^ "]*api/public/otel'
# http://langfuse.langfuse.svc.cluster.local:3000/api/public/otel
```

---

## 6. A score lands — exactly one per trace

```bash
curl -s -u "$PK:$SK" "$LF/api/public/v2/scores?limit=10" \
  | jq -r '.data[] | "\(.timestamp) \(.name)=\(.value) \(.comment[0:70])"'
```

Expect one row per request, e.g.
`helpfulness=1 The response is concise, specific, and directly answers...`

**One** score per trace matters. A single agent turn emits ~28 observations, and
the rule filters to the one of `type=AGENT` — the agent-invocation span that
holds the request and the final answer. If you see three scores per trace the
type filter is missing; if you see a score of 0 commented *"No user request or
agent response provided"* the rule is judging the a2a transport span instead.

To see which rule produced a score:

```bash
curl -s -u "$PK:$SK" "$LF/api/public/v2/scores?limit=5" \
  | jq -r '.data[] | .metadata.job_configuration_id'
```

---

## 7. Metrics and the dashboard

```bash
curl -s http://127.0.0.1:9098/metrics | grep -E '^oap_agent_eval_(scores|passed|pass_ratio|pass_threshold|info)'
```

Expect, for `agent="eval-smoketest"`:

```
oap_agent_eval_scores{...,window="24h"}          33
oap_agent_eval_passed{...,window="24h"}          24
oap_agent_eval_pass_ratio{...,window="24h"}      0.727
oap_agent_eval_pass_threshold{...}               0.7
oap_agent_eval_info{...,eval_ai_request_model="judge-default",gen_ai_request_model="claude-sonnet"} 1
```

`pass_threshold` 0.7 is `helpfulness` at `strictness: medium`, resolved from the
platform catalog — not from anything the Application said. Change the agent to
`strictness: high` and it becomes 0.85 with no new Langfuse rule, because
strictness is applied when the ratio is computed.

An **idle** agent has no `pass_ratio` series at all, rather than 0. That is
deliberate: 0 would read as "every answer failed" instead of "no traffic".

Dashboard: AMG → **Agent Platform** → **Agent Evaluation**. It is imported by
grafana-operator, so it appears without a manual import:

```bash
kubectl get grafanadashboard agent-evaluation -n grafana-operator
```

Metrics reach AMP through the scrape annotations on
`svc/agent-evaluation-metrics`, so allow a scrape interval before the panels
fill.

---

## 8. Compare the two judges (optional)

The judge choice is measurable rather than theoretical. Set on any agent:

```yaml
evaluation:
  judge:
    modelId: judge-haiku     # or judge-default
```

The dashboard breaks pass ratios down by judge model, and
`oap_agent_eval_info.eval_ai_request_model` carries it. Note `judge-haiku` is
the same family as the agents, so it reintroduces the self-preference bias
`judge-default` (Nova Pro, a different family) avoids.

---

## 9. See all four dimensions at once

§4–§7 show one evaluator, because both smoke tests ask for one. To see what a
real agent's evaluation looks like — four score names per trace, four
thresholds, a per-evaluator override — apply the third example:

```bash
kubectl apply -f platform/oam/examples/example-agent-evaluation-multidim.yaml
```

It is built to not disturb anything already running:

| Choice | Why |
|---|---|
| name `eval-multidim` | new trace tag, so no existing rule's filter changes |
| `judge: judge-haiku` | lands on the `*--judge-haiku` evaluator variants, so it gets **four new rules** instead of joining the shared `oap-<token>-helpfulness-<hash>` |
| `sampling: 1` | all four scores land on the first request |

Drop the `judge` block and `helpfulness` is instead added as a third tag on
that shared rule. That is the designed grouping behaviour and harmless, but it
mutates an object the other two agents depend on — not what you want mid-test.

Drive it the same way as §4 (A2A, not `/chat`):

```bash
kubectl port-forward -n agents svc/eval-multidim-stable 8085:8083 &
curl -s -X POST http://127.0.0.1:8085/ -H 'Content-Type: application/json' -d '{
  "jsonrpc":"2.0","id":"3","method":"message/send",
  "params":{"message":{"role":"user","kind":"message","messageId":"m3",
    "parts":[{"kind":"text","text":"In one sentence, what does a Kubernetes Service do?"}]}}}'
```

Expect **four** scores on that one trace. Measured on the hub:

```bash
curl -s -u "$PK:$SK" "$LF/api/public/v2/scores?limit=10" \
  | jq -r '.data[] | "\(.name)=\(.value)"'
# helpfulness--judge-haiku=1
# goal-completion--judge-haiku=1
# safety--judge-haiku=1
# tool-use--judge-haiku=1
```

Note the score names carry the `--judge-haiku` suffix, because the judge alias
is part of the evaluator object's name. On the default judge they would be bare
`helpfulness`, `safety` and so on — so a dashboard or query that hardcodes a
score name is judge-specific whether or not it meant to be.

And five rules in total, four of them new:

```bash
curl -s -u "$PK:$SK" "$LF/api/public/v2/evaluation-rules?limit=20" | jq -r '.data[].name'
```

Three things that look like bugs and are not:

- **Cost and result stay empty until you send traffic.** A rule scores
  observations that arrive *after* it exists, so four freshly created rules sit
  at "no value" however long you wait. Drive a request, then look again.
- **`tool-use` scores 1 on a toolless agent.** The judge grades whether tool use
  was *appropriate*, not whether a tool was called, so correctly declining to
  call one earns full marks: "the user asked a general conceptual question". A
  low score here means the agent should have reached for a tool and didn't.
- **`safety: strictness high` adds no rule.** Strictness changes the reported
  `pass_threshold` (0.97 instead of 0.9), which is applied when the pass ratio
  is computed — not when the judge is called. Only `(evaluator, judge, sampling)`
  creates rules. Confirm with:

```bash
curl -s http://127.0.0.1:9098/metrics | grep 'oap_agent_eval_pass_threshold.*eval-multidim'
```

Catalog thresholds, for reading those numbers:

| Evaluator | low | medium | high |
|---|---|---|---|
| `goal-completion` | 0.5 | 0.7 | 0.85 |
| `helpfulness` | 0.5 | 0.7 | 0.85 |
| `tool-use` | 0.5 | 0.7 | 0.85 |
| `safety` | 0.8 | 0.9 | 0.97 |

Coverage presets map to sampling `low` 1% / `medium` 10% / `high` 50%.

Tear down with `kubectl delete -f` on the same file; the four rules disappear
once no agent needs them.

---

## What was verified on the hub

Both examples, end to end, on 2026-10-30:

| | Pinned image | Imageless |
|---|---|---|
| Declaration ConfigMap rendered | yes | yes |
| Rule created by the reconciler | yes | yes |
| Image used | `…/strands-agent:eval-tracetag` | `…/strands-agent:v1.3.4-tracetag` (platform default) |
| Scoped trace tag | `agent:hub/agents/eval-smoketest` | `agent:hub/agents/eval-imageless` |
| Score from the judge | `helpfulness = 1` | `helpfulness = 1` |

## Tearing the smoke tests down

```bash
kubectl delete -f platform/oam/examples/example-agent-evaluation-smoketest.yaml
kubectl delete -f platform/oam/examples/example-agent-evaluation-imageless.yaml
```

The declaration ConfigMap is a KubeVela output, so deleting the Application
deletes it, and the reconciler drops the agent from the rule on its next pass —
removing the rule entirely once no agent needs it. No finalizer involved. Verify:

```bash
curl -s -u "$PK:$SK" "$LF/api/public/v2/evaluation-rules?limit=10" | jq -r '.data[].name'
```

## Unit tests

```bash
cd applications/agent-evaluation
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest tests/ -q      # 73 tests, no cluster needed
```

## Re-verifying the Langfuse API after an upgrade

`live_check.py` runs the real loops against a real project and reports each step,
so API drift shows up as a named failure instead of a silent one. See the README.

---

# Appendix A — inspect every object, one command at a time

A read-only inventory of the whole feature. Nothing here changes state: every
command is `get`, `describe`, `logs` or an HTTP `GET`. Work down it and you have
seen every object the feature consists of.

Ordered by dependency — catalog before reconciler, reconciler before rules,
rules before scores — so the first thing that looks wrong is usually the cause
of everything below it, not a separate problem.

## A.0 Set up once

```bash
export EVAL_NS=agent-evaluation     # where the reconciler runs
export AGENT_NS=agents              # where the agents run
export CLUSTER=hub                  # must match global.clusterName

# the reconciler's own HTTP surface
kubectl port-forward -n $EVAL_NS svc/agent-evaluation-metrics 9098:9090 &
export EV=http://127.0.0.1:9098

# the Langfuse public API (same two vars as the top of this document)
kubectl port-forward -n langfuse svc/langfuse 3999:3000 &
export LF=http://127.0.0.1:3999
export PK=$(aws secretsmanager get-secret-value --secret-id peeks-hub/langfuse-otel \
  --query SecretString --output text | jq -r .LANGFUSE_PUBLIC_KEY)
export SK=$(aws secretsmanager get-secret-value --secret-id peeks-hub/langfuse-otel \
  --query SecretString --output text | jq -r .LANGFUSE_SECRET_KEY)
```

`$PK`/`$SK` are the project's API key pair. They are in your shell, so prefer
`history -d` over leaving them in a shared terminal, and never paste the values
into a ticket or a PR.

## A.1 Is the addon installed on this cluster at all

The addon is opt-in per cluster, keyed off a cluster-secret annotation, so
"nothing is running" is more often "never selected" than "crashed".

```bash
# the ArgoCD Application that renders the chart
kubectl get applications -n argocd | grep agent-evaluation

# the annotation that selects it (enable_agent_evaluation: 'true')
kubectl get secret -n argocd -l argocd.argoproj.io/secret-type=cluster \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.metadata.annotations.enable_agent_evaluation}{"\t"}{.metadata.annotations.ingress_domain_name}{"\n"}{end}'
```

`ingress_domain_name` in the third column is what makes a cluster the **hub**:
the registry entry derives `role: hub` from a non-empty value and `role: spoke`
from an empty one. Exactly one cluster should be hub.

```bash
# everything the chart created, in one listing
kubectl get all,configmap,secret,externalsecret,prometheusrule -n $EVAL_NS
```

Expected: one Deployment, one ReplicaSet, one Pod, one Service, the
`agent-evaluation-catalog` ConfigMap, the `langfuse-evaluation-keys` Secret, the
`agent-evaluation-langfuse` ExternalSecret, and the `agent-evaluation`
PrometheusRule.

## A.2 The reconciler

```bash
kubectl get deploy,rs,pod -n $EVAL_NS -l app=agent-evaluation-reconciler
kubectl describe deploy agent-evaluation-reconciler -n $EVAL_NS
```

`app=agent-evaluation-reconciler` is the selector label; the `app.kubernetes.io/*`
labels are on the objects too but are not the selector.

One replica and `strategy: Recreate` are both deliberate — two reconcilers would
PATCH the same Langfuse rules. A `replicas: 2` here is a misconfiguration, not
extra safety.

**Which image it is actually running**, which is worth checking first on a fresh
cluster, because the published tag may not exist yet:

```bash
kubectl get deploy agent-evaluation-reconciler -n $EVAL_NS \
  -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}'
kubectl get pod -n $EVAL_NS -l app=agent-evaluation-reconciler \
  -o jsonpath='{.items[0].status.containerStatuses[0].imageID}{"\n"}'
```

`ImagePullBackOff` with `manifest unknown` means the tag is not published;
`repository does not exist` means the repo itself is not there. The chart default
is `public.ecr.aws/z0a4o2j5/agent-evaluation`.

**Its configuration**, all of which arrives as environment:

```bash
kubectl set env deploy/agent-evaluation-reconciler -n $EVAL_NS --list
```

| Variable | Meaning |
|---|---|
| `ROLE` | `hub` or `spoke`. A hub also owns the project-wide objects. |
| `CLUSTER_NAME` | scopes rule names, trace tags and metric series |
| `LANGFUSE_URL` | in-cluster Service on the hub, HTTPS hub domain on a spoke |
| `BIFROST_URL` | gateway the judge is called through |
| `RECONCILE_INTERVAL_SECONDS` | rules/project loop period (default 60) |
| `METRICS_INTERVAL_SECONDS` | score-aggregation period (default 300) |
| `METRICS_WINDOWS` | rolling windows reported (default `24h,7d`) |
| `METRICS_PORT` | listener port (default 9090) |
| `MAX_ACTIVE_RULES` | per-project rule cap treated as the contract (50) |
| `CATALOG_PATH` | `/etc/agent-evaluation/catalog.yaml` |
| `CREDENTIALS_DIR` | `/etc/agent-evaluation/credentials` |

**Its three loops.** `rules` and `metrics` run everywhere; `project` runs only on
a hub. Readiness is per loop:

```bash
curl -s $EV/readyz | jq
curl -s -o /dev/null -w '%{http_code}\n' $EV/readyz   # 200 ready, 503 not
curl -s $EV/healthz | jq                              # always 200 by design
```

Ready on a hub needs **both** `rules` and `project`. A `waitingOn` key names the
loop and the reason. `/healthz` never consults Langfuse, so a slow dependency
cannot turn into a restart loop — which is why a pod can be `Running` and
`0/1 READY` for minutes on a first install and that is correct.

```bash
# startup banner: role, cluster, owner token, evaluator count, judge aliases
kubectl logs -n $EVAL_NS deploy/agent-evaluation-reconciler | head -5

# only the passes that changed something; silence means "already correct"
kubectl logs -n $EVAL_NS deploy/agent-evaluation-reconciler --tail=200 \
  | grep -E "created|updated|deleted|skipp|error|WARN"

# follow one pass
kubectl logs -n $EVAL_NS deploy/agent-evaluation-reconciler -f
```

The reconciler is level-triggered: it logs only on change. An idle log is the
expected steady state, not a stuck loop — use `oap_agent_eval_last_success_*`
(A.12) to tell "idle" from "stuck".

## A.3 ConfigMap 1 of 2 — the catalog (platform policy)

One per cluster, rendered from chart values, holding the judge prompts, the
coverage rates and the judge aliases. This is the file that makes a prompt change
a reviewed chart change rather than a per-agent setting.

```bash
kubectl get configmap agent-evaluation-catalog -n $EVAL_NS

# the whole thing
kubectl get configmap agent-evaluation-catalog -n $EVAL_NS \
  -o jsonpath='{.data.catalog\.yaml}'

# just the structure: evaluator names, coverage rates, judge aliases
kubectl get configmap agent-evaluation-catalog -n $EVAL_NS \
  -o jsonpath='{.data.catalog\.yaml}' \
  | yq '{"evaluators": (.catalog|keys), "coverage": .coverage, "judge": .judge}'

# thresholds per evaluator per strictness
kubectl get configmap agent-evaluation-catalog -n $EVAL_NS \
  -o jsonpath='{.data.catalog\.yaml}' \
  | yq '.catalog | to_entries | map({"name": .key, "thresholds": .value.passThreshold})'

# one judge prompt as the hub publishes it
kubectl get configmap agent-evaluation-catalog -n $EVAL_NS \
  -o jsonpath='{.data.catalog\.yaml}' | yq -r '.catalog.helpfulness.prompt'
```

Expect four evaluators (`goal-completion`, `helpfulness`, `safety`, `tool-use`),
coverage `low: 0.01 / medium: 0.1 / high: 0.5`, and two judge aliases.

Do not edit this in the cluster — the next Argo sync overwrites it. A catalog
change is a chart change, and the Deployment carries a `checksum/catalog`
annotation so the edit actually restarts the pod:

```bash
kubectl get deploy agent-evaluation-reconciler -n $EVAL_NS \
  -o jsonpath='{.spec.template.metadata.annotations.checksum/catalog}{"\n"}'
```

If that checksum is unchanged after a chart bump, the prompt edit did not reach
the pod and the hub is still publishing the old prompt.

## A.4 ConfigMap 2 of 2 — the per-agent declarations

One per agent with evaluation on, rendered by the `agent` component as a KubeVela
output, named `<agent>-evaluation`, labelled `evaluation.oap.io/agent`. The
reconciler lists on that label **alone** — an agent whose ConfigMap lacks it is
invisible to the feature.

```bash
# every declaration on the cluster, all namespaces
kubectl get configmap -A -l evaluation.oap.io/agent

# what each one asks for, as a table
kubectl get configmap -A -l evaluation.oap.io/agent -o json | jq -r '
  .items[] | [.metadata.namespace, .metadata.name,
               .data.evaluators, (.data.strictness // "-"),
               (.data.coverage // "-"), (.data.sampling // "-"),
               (.data.judge // "(default)")] | @tsv' | column -t

# one agent in full
kubectl get configmap eval-smoketest-evaluation -n $AGENT_NS -o yaml
```

The data keys, and who reads them:

| Key | Set from | Used for |
|---|---|---|
| `agent` | component name | the series and the trace tag |
| `backend` | `observability.mode` | `centralized` = Langfuse |
| `model` | `modelConfig.modelId` | comparing quality by agent model |
| `strictness` | `evaluation.strictness` | resolves the pass **threshold** only |
| `coverage` | `evaluation.coverage` | preset sampling rate |
| `evaluators` | JSON list | names, or `{name, strictness}` overrides |
| `sampling` | optional | overrides `coverage`; must be in (0, 1] |
| `judge` | optional | absent means the platform default judge |

Two keys are commonly misread. `strictness` creates **no** Langfuse rule — it is
applied when the pass ratio is computed. Only `(evaluator, judge, sampling)`
groups into rules, which is why four agents can share one rule.

An agent with **no** `evaluation` block has no ConfigMap here at all. That is the
opt-in working; `my-agent` on the hub is the example.

## A.5 Credentials (presence, never values)

```bash
kubectl get externalsecret -n $EVAL_NS
kubectl describe externalsecret agent-evaluation-langfuse -n $EVAL_NS

# which keys landed — names only
kubectl get secret langfuse-evaluation-keys -n $EVAL_NS \
  -o jsonpath='{range $k,$v := .data}{$k}{"\n"}{end}'
```

Expect `SyncedReady=True` and the keys `LANGFUSE_PUBLIC_KEY` and
`LANGFUSE_SECRET_KEY`. A `SecretSyncedError` here is the usual cause of a
reconciler sitting at 401 — the langfuse addon's key-push hook has not run yet.
The ExternalSecret refreshes every 1m for exactly that reason.

Do not `base64 -d` these into your terminal. The reconciler reads them as mounted
files rather than env vars precisely so they do not show up in a
`kubectl describe pod`:

```bash
kubectl exec -n $EVAL_NS deploy/agent-evaluation-reconciler -- \
  ls -l /etc/agent-evaluation/credentials/
```

The judge virtual key is **optional** and off by default — Bifrost runs with
`is_vk_mandatory: false`, so without it the judge still works and only loses
budget attribution:

```bash
kubectl get externalsecret agent-evaluation-judge-key -n $EVAL_NS 2>/dev/null \
  || echo "judge virtual key disabled (expected default)"
curl -s $EV/metrics | grep oap_agent_eval_judge_key_present
```

## A.6 RBAC

```bash
kubectl get sa agent-evaluation-reconciler -n $EVAL_NS
kubectl describe clusterrole agent-evaluation-reconciler
kubectl get clusterrolebinding agent-evaluation-reconciler -o yaml
```

Cluster-wide ConfigMap `get/list/watch` is intentional and is the broadest grant
in the chart: agents' ConfigMaps live in every team's namespace and Kubernetes
RBAC cannot scope a list by label. It is narrowed everywhere else — configmaps
only, read only, and the code always lists with the label selector. Confirm the
grant is actually read-only:

```bash
kubectl auth can-i --as=system:serviceaccount:$EVAL_NS:agent-evaluation-reconciler \
  list configmaps --all-namespaces      # yes
kubectl auth can-i --as=system:serviceaccount:$EVAL_NS:agent-evaluation-reconciler \
  delete configmaps --all-namespaces    # no
kubectl auth can-i --as=system:serviceaccount:$EVAL_NS:agent-evaluation-reconciler \
  create events -n $AGENT_NS            # yes
```

A `yes` on that middle one is a finding — report it rather than working around it.

## A.7 The agents

```bash
kubectl get applications.core.oam.dev -n $AGENT_NS
kubectl get pods,svc -n $AGENT_NS

# which image each agent pod actually resolved to
kubectl get pods -n $AGENT_NS -o json | jq -r '
  .items[] | "\(.metadata.name)\t\(.spec.containers[0].image)"' | column -t

# the platform default an imageless Application falls back to
kubectl get componentdefinition agent -n vela-system -o yaml \
  | grep -oE 'image: \*\\"[^\\]*'
```

This is the check that catches the quiet failure. The scoped trace tag is emitted
by the **image**, so an imageless agent running a pre-tag default image is
configured for evaluation and never scored, with nothing erroring anywhere.

**The three env vars that make the tag**, read off a running pod:

```bash
kubectl get pod -n $AGENT_NS -l app.kubernetes.io/name=eval-imageless -o json | jq -r '
  .items[0].spec.containers[0].env[]
  | select(.name=="CLUSTER_NAME" or .name=="AGENT_NAMESPACE" or .name=="AGENT_NAME")
  | "\(.name)=\(.value // "(fieldRef)")"'
```

All three must be present. The agent composes `agent:<cluster>/<ns>/<name>` from
them and emits **no** scoped tag if the cluster or namespace half is missing — a
local run deliberately emits nothing rather than a half-formed tag that would
match a real cluster's rule.

## A.8 Langfuse — the evaluators (the hub publishes these)

```bash
curl -s $LF/api/public/health | jq        # expect "version":"4.x"

curl -s -u "$PK:$SK" "$LF/api/public/v2/evaluators?limit=100" | jq -r '
  .data[] | "\(.name)\tv\(.version)\t\(.status)\t\(.modelConfig.provider)\t\(.modelConfig.model)"' \
  | column -t
```

Expect every row `active`. What to read in each column:

- **name** — bare (`helpfulness`) for the default judge, `<evaluator>--<alias>`
  (`helpfulness--judge-haiku`) for any other. The judge is a property of the
  *evaluator*, not the rule, so there is one object per catalog entry per alias.
- **status** — a `paused` row with `EVAL_MODEL_CONFIG_INVALID` means that judge
  model failed Langfuse's structured-output preflight (§2). It scores nothing.
- **provider** — `oap-bifrost-judge` is the ownership marker. In v4 a name is not
  an identifier, so "is this evaluator ours?" is answered by the provider, not
  the name. An evaluator of ours without it will be duplicated on the next pass.

```bash
# version history for one evaluator — a prompt change mints a new version
EV_ID=$(curl -s -u "$PK:$SK" "$LF/api/public/v2/evaluators?limit=100" \
  | jq -r '.data[] | select(.name=="helpfulness") | .id')
curl -s -u "$PK:$SK" "$LF/api/public/v2/evaluators/$EV_ID/versions?limit=100" \
  | jq -r '.data[] | "v\(.version)\t\(.createdAt)"'

# duplicate detection: any name appearing twice is a bug worth reporting
curl -s -u "$PK:$SK" "$LF/api/public/v2/evaluators?limit=100" \
  | jq -r '.data[].name' | sort | uniq -d
```

Four catalog evaluators and two judge aliases mean up to eight evaluator objects,
while rules exist only for what agents actually asked for. Published-and-unused
is normal: evaluators are definitions, rules are usage.

> **Pagination.** The `v2` list endpoints cursor-paginate — `limit` caps at 100
> and the next page needs `meta.cursor`, which is omitted on the last page.
> `?limit=100` alone silently truncates past 100 objects:
> ```bash
> curl -s -u "$PK:$SK" "$LF/api/public/v2/evaluators?limit=100" | jq '.meta'
> ```
> A non-null `cursor` there means you are not seeing everything.

## A.9 Langfuse — the judge LLM connection (hub only)

```bash
# the whole object, so you see whatever field names this Langfuse version uses
curl -s -u "$PK:$SK" "$LF/api/public/llm-connections" | jq '.data'

# just the provider names
curl -s -u "$PK:$SK" "$LF/api/public/llm-connections" | jq -r '.data[].provider'
```

Expect a `oap-bifrost-judge` connection pointing at the Bifrost Service URL and
listing every judge model id from the catalog. Langfuse will only accept an
evaluator whose model is listed here, so a judge alias added to chart values but
missing from this connection is the failure to look for.

Unlike the `v2` endpoints this one is page-numbered (`page`/`meta.totalPages`).

## A.10 Langfuse — the evaluation rules (each cluster owns its own)

```bash
curl -s -u "$PK:$SK" "$LF/api/public/v2/evaluation-rules?limit=100" \
  | jq '.data[] | {name, enabled, sampling, evaluator: .evaluator.name, filter}'

# just the shape: name, sampling, and which agents each rule covers
curl -s -u "$PK:$SK" "$LF/api/public/v2/evaluation-rules?limit=100" | jq -r '
  .data[] | "\(.name)\tsampling=\(.sampling)\tenabled=\(.enabled)\t\(
    [.filter[]? | select(.column=="tags") | .value[]?] | join(","))"' | column -t
```

Read a rule as four things:

- **name** `oap-<owner-token>-<evaluator>-<config-hash>` — see A.15 to derive it.
- **sampling** the fraction of matching observations judged.
- **filter** `type any of [AGENT]` plus `tags any of [...]`. The `AGENT` type
  filter is load-bearing: one agent turn emits ~28 observations, and without it
  you get a score per span instead of one per turn.
- **one rule, many tags** — rules group by `(evaluator, judge, sampling)`, so
  agents asking for the same three share a rule and appear as sibling tags.
  Change one agent's `sampling` or `judge` and it splits off into its own rule.

```bash
# rules this cluster owns, vs. another cluster's (never touch those)
MY=$(python3 -I -c "import hashlib,os;print(hashlib.sha256(os.environ['CLUSTER'].encode()).hexdigest()[:8])")
curl -s -u "$PK:$SK" "$LF/api/public/v2/evaluation-rules?limit=100" \
  | jq -r --arg t "$MY" '.data[].name | select(startswith("oap-"))
      | if startswith("oap-" + $t + "-") then "MINE  " + . else "OTHER " + . end'

# against the cap (50 per project, fleet-wide)
curl -s $EV/metrics | grep oap_agent_eval_rules_active
```

The owner token is an exact-length hash, not the cluster name, so one cluster's
prefix can never match another's — the reason `dev` cannot delete `dev-2`'s rules.

## A.11 Langfuse — traces and scores

```bash
# do traces arrive, and do they carry the scoped tag
curl -s -u "$PK:$SK" "$LF/api/public/traces?limit=5" \
  | jq -r '.data[] | "\(.timestamp)\t\(.name)\t\(.tags|join(","))"' | column -t

# scores, newest first
curl -s -u "$PK:$SK" "$LF/api/public/v2/scores?limit=20" \
  | jq -r '.data[] | "\(.timestamp)\t\(.name)=\(.value)\t\(.comment[0:60])"' | column -t

# which rule produced each score
curl -s -u "$PK:$SK" "$LF/api/public/v2/scores?limit=20" \
  | jq -r '.data[] | "\(.name)\t\(.metadata.job_configuration_id)"' | column -t

# count scores per name, to see all dimensions at once
curl -s -u "$PK:$SK" "$LF/api/public/v2/scores?limit=100" \
  | jq -r '.data[].name' | sort | uniq -c | sort -rn
```

Three absences and what each means:

| Symptom | Where the chain broke |
|---|---|
| no traces | collector cannot reach Langfuse (§5) |
| traces, no scoped tag | agent image predates the tag (A.7) |
| scoped tag, no scores | no rule matches it, or the judge is paused (A.8/A.10) |
| scores, but 3+ per trace | the `type=AGENT` filter is missing from the rule |

A rule only judges observations that arrive **after** it exists, so a
freshly-created rule reports nothing however long you wait. Send a request first.

## A.12 The metrics endpoint

```bash
# every family the reconciler publishes
curl -s $EV/metrics | grep '^# HELP oap_'

# everything, values included
curl -s $EV/metrics | grep '^oap_'
```

| Metric | Labels | Reads as |
|---|---|---|
| `oap_agent_eval_scores` | +`window` | traces scored in the window |
| `oap_agent_eval_passed` | +`window` | at or above the threshold |
| `oap_agent_eval_pass_ratio` | +`window` | passed / scores |
| `oap_agent_eval_score_avg` | +`window` | mean score |
| `oap_agent_eval_pass_threshold` | no window | resolved from `strictness` |
| `oap_agent_eval_info` | agent + judge model, strictness, coverage | always 1; join in PromQL |
| `oap_agent_eval_rules_active` | `cluster` | against the 50 cap |
| `oap_agent_eval_agents_declared` | `cluster` | agents with evaluation on |
| `oap_agent_eval_agents_skipped` | `cluster` | declarations that failed to reconcile |
| `oap_agent_eval_errors_total` | `cluster`,`loop` | failed Langfuse calls, per loop |
| `oap_agent_eval_last_success_timestamp_seconds` | `cluster`,`loop` | liveness of each loop |
| `oap_agent_eval_cluster_info` | +`owner_token`,`role` | maps a token to a cluster |
| `oap_agent_eval_judge_key_present` | `cluster` | 0 = judge runs un-budgeted |

```bash
# per-agent quality
curl -s $EV/metrics | grep -E '^oap_agent_eval_(scores|passed|pass_ratio|score_avg)'

# thresholds, which come from strictness and not from a window
curl -s $EV/metrics | grep '^oap_agent_eval_pass_threshold'

# are the loops alive — compare to `date +%s`
curl -s $EV/metrics | grep '^oap_agent_eval_last_success_timestamp_seconds'; date +%s

# anything skipped, and errors by loop
curl -s $EV/metrics | grep -E '^oap_agent_eval_(agents_skipped|errors_total)'
```

Two readings that look like faults and are not. An **idle** agent has no
`pass_ratio` series at all rather than 0 — a 0 would read as "every answer
failed". And a non-zero `errors_total` that is not growing is a healed transient;
the figure to watch is `last_success_timestamp_seconds` falling behind
`METRICS_INTERVAL_SECONDS`, which is what the staleness alert fires on.

The AMP scraper finds this endpoint through annotations on the Service, not a
ServiceMonitor:

```bash
kubectl get svc agent-evaluation-metrics -n $EVAL_NS \
  -o jsonpath='{.metadata.annotations}' | jq
kubectl get endpoints agent-evaluation-metrics -n $EVAL_NS
```

An empty `endpoints` means the Service selector matches no ready pod — the
metrics exist in the pod and nothing is scraping them.

## A.13 Dashboard and alerts

```bash
kubectl get grafanadashboard agent-evaluation -n grafana-operator
kubectl describe grafanadashboard agent-evaluation -n grafana-operator | tail -20
```

It is imported by grafana-operator into the **Agent Platform** folder, so it
appears without a manual import. AMG → Agent Platform → Agent Evaluation.

```bash
kubectl get prometheusrule agent-evaluation -n $EVAL_NS
kubectl get prometheusrule agent-evaluation -n $EVAL_NS \
  -o jsonpath='{range .spec.groups[*].rules[*]}{.alert}{"\t"}{.for}{"\n"}{end}'
```

Four alerts, and all four are about the **pipeline**, never about agent quality:

| Alert | Fires when |
|---|---|
| `AgentEvaluationDataStale` | no successful pass within `alerts.dataAgeSeconds` |
| `AgentEvaluationReconcileErrors` | Langfuse calls failing in a loop |
| `AgentEvaluationRuleBudgetHigh` | active rules past 80% of the cap |
| `AgentEvaluationJudgeKeyMissing` | the judge is running un-budgeted |

A falling pass ratio deliberately does not page: a strict evaluator on a hard
task is *supposed* to score low, and that is the owning team's signal to read on
the dashboard, not the platform's to wake someone for.

## A.14 Events — what a team sees without platform access

Events are written onto the agent's own ConfigMap, in the agent's namespace, so a
team diagnoses their own declaration with no access to the reconciler's logs.

```bash
kubectl get events -n $AGENT_NS --field-selector involvedObject.kind=ConfigMap \
  --sort-by=.lastTimestamp

kubectl describe configmap eval-smoketest-evaluation -n $AGENT_NS | tail -20
```

| Reason | Means |
|---|---|
| `EvaluationReconciled` | normal; the declaration is in effect |
| `UnknownEvaluator` | name is not in the catalog (A.3) |
| `EvaluatorNotPublished` | in the catalog, but the hub has not published it |
| `UnknownJudge` | `judge.modelId` is not a chart judge alias |
| `InvalidEvaluationDeclaration` | malformed — bad `sampling`, duplicate evaluator |
| `EvaluationRuleLimitReached` | project is at the rule cap |

They are deduplicated by (object, reason, message) with a count, so a persistent
problem shows a rising count rather than one Event per 60s pass.

## A.15 Deriving the names yourself

Langfuse object names are hashes, which is good for safety and bad for reading.
Both are reproducible locally:

```bash
# owner token = first 8 hex of sha256(cluster name)
python3 -I -c 'import hashlib,sys;print(hashlib.sha256(sys.argv[1].encode()).hexdigest()[:8])' hub
# 08d33503

# config hash = first 8 hex of sha256("<judge>\0<sampling to 6dp>")
python3 -I -c 'import hashlib;print(hashlib.sha256(b"judge-default\x001.000000").hexdigest()[:8])'
# 8c49ba7c

# so the rule is: oap-08d33503-helpfulness-8c49ba7c
```

Or read the mapping straight off the metrics, which is why
`oap_agent_eval_cluster_info` carries the token as a label:

```bash
curl -s $EV/metrics | grep oap_agent_eval_cluster_info
```

## A.16 One pass over everything

Read-only; prints the whole inventory in order. Requires A.0 to have been run.

```bash
cat <<'EOS' > /tmp/eval-inventory.sh
#!/usr/bin/env bash
# Read-only inventory of continuous agent evaluation. No writes.
set -uo pipefail
h() { printf '\n\033[1m== %s\033[0m\n' "$1"; }

h "reconciler";      kubectl get deploy,pod,svc -n "$EVAL_NS" -l app=agent-evaluation-reconciler
h "image";           kubectl get deploy agent-evaluation-reconciler -n "$EVAL_NS" \
                       -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}'
h "readyz";          curl -s "$EV/readyz" | jq -c
h "catalog";         kubectl get cm agent-evaluation-catalog -n "$EVAL_NS" \
                       -o jsonpath='{.data.catalog\.yaml}' \
                       | yq -c '{"evaluators":(.catalog|keys),"coverage":.coverage,"judge":.judge}'
h "declarations";    kubectl get cm -A -l evaluation.oap.io/agent -o json | jq -r '
                       .items[] | [.metadata.namespace,.metadata.name,.data.evaluators,
                                   (.data.sampling//"-"),(.data.judge//"(default)")] | @tsv'
h "credentials";     kubectl get externalsecret -n "$EVAL_NS"
h "agents";          kubectl get applications.core.oam.dev -n "$AGENT_NS"
h "agent images";    kubectl get pods -n "$AGENT_NS" -o json | jq -r '
                       .items[] | "\(.metadata.name)\t\(.spec.containers[0].image)"'
h "evaluators";      curl -s -u "$PK:$SK" "$LF/api/public/v2/evaluators?limit=100" | jq -r '
                       .data[] | "\(.name)\t\(.status)\t\(.modelConfig.model)"'
h "llm connection";  curl -s -u "$PK:$SK" "$LF/api/public/llm-connections" | jq -r '.data[].provider'
h "rules";           curl -s -u "$PK:$SK" "$LF/api/public/v2/evaluation-rules?limit=100" | jq -r '
                       .data[] | "\(.name)\tsampling=\(.sampling)\t\([.filter[]?
                         | select(.column=="tags") | .value[]?] | join(","))"'
h "recent scores";   curl -s -u "$PK:$SK" "$LF/api/public/v2/scores?limit=10" | jq -r '
                       .data[] | "\(.name)=\(.value)"'
h "metrics";         curl -s "$EV/metrics" | grep -E '^oap_agent_eval_(pass_ratio|rules_active|agents_declared|agents_skipped|judge_key_present)'
h "alerts";          kubectl get prometheusrule agent-evaluation -n "$EVAL_NS" \
                       -o jsonpath='{range .spec.groups[*].rules[*]}{.alert}{"\n"}{end}'
h "events";          kubectl get events -n "$AGENT_NS" \
                       --field-selector involvedObject.kind=ConfigMap --sort-by=.lastTimestamp \
                       | tail -10
EOS
bash /tmp/eval-inventory.sh
```

Reading the output top to bottom: a blank **declarations** block with healthy
everything else means no agent has opted in; declarations present but no
**rules** means the reconciler cannot write to Langfuse; rules present but no
**scores** means either no traffic or no trace carrying the matching tag.
