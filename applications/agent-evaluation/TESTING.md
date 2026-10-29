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

## 3. Turn evaluation on for an agent

A ready-made smoke test is committed:

```bash
kubectl apply -f platform/oam/examples/example-agent-evaluation-smoketest.yaml
```

It uses `sampling: 1` and a single evaluator so a score appears in about a
minute. Both are wrong for production — judging every trace costs a judge call
per request, which is what `coverage` exists to control.

Check the component rendered the declaration:

```bash
kubectl get configmap eval-smoketest-evaluation -n agents -o jsonpath='{.data}' | jq
```

Then watch the reconciler turn it into a rule (within one 60s pass):

```bash
kubectl logs -n agent-evaluation deploy/agent-evaluation-reconciler --tail=50 | grep -E "created rule|updated rule"
# created rule oap-08d33503-helpfulness-8c49ba7c (evaluator=helpfulness judge=judge-default sampling=1.0 agents=1)
```

`oap-08d33503-…` — the second segment is this cluster's owner token, not its
name. Map it with:

```bash
curl -s http://127.0.0.1:9098/metrics | grep oap_agent_eval_cluster_info
```

---

## 4. Drive traffic — over A2A, not /chat

**Use the A2A endpoint.** `/chat` answers correctly but is not the traced path,
so it produces no trace and nothing to score. This cost an hour the first time.

```bash
kubectl port-forward -n agents svc/eval-smoketest-stable 8083:8083 &
curl -s -X POST http://127.0.0.1:8083/ -H 'Content-Type: application/json' -d '{
  "jsonrpc":"2.0","id":"1","method":"message/send",
  "params":{"message":{"role":"user","kind":"message","messageId":"m1",
    "parts":[{"kind":"text","text":"In one sentence, what is Kubernetes RBAC for?"}]}}}'
```

---

## 5. The trace carries the cluster-scoped tag

```bash
curl -s -u "$PK:$SK" "$LF/api/public/traces?limit=3" | jq -r '.data[] | "\(.timestamp) \(.tags)"'
```

Expect `["agent:hub/agents/eval-smoketest","eval-smoketest","source:chat"]`.

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

## Tearing the smoke test down

```bash
kubectl delete -f platform/oam/examples/example-agent-evaluation-smoketest.yaml
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
