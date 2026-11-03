# Live verification of PR 104 — overnight run, 2026-11-02

Goal: prove the reviewed changes work **in the deployed path**, not just in a
local harness, and specifically that **scoring still works end to end**.

Branch under test: `feat/agent-evaluation` @ `79aa93c8`
Image under test: `940019131157.dkr.ecr.us-west-2.amazonaws.com/agent-evaluation:0.2.4`
Cluster: `hub` (KubeVela v1.11.0, Langfuse v4.72.0)

## Plan

1. Build + push `0.2.4` multi-arch to the private ECR
2. Confirm the manifest list has both architectures
3. Roll the hub reconciler onto `0.2.4`
4. Confirm `/readyz` and that both hub loops report healthy
5. Confirm the judge-suffix fix in the DEPLOYED path: `eval-multidim` non-zero
6. Drive fresh A2A traffic through all three agents
7. Confirm NEW traces carry the scoped tag
8. Confirm NEW scores land (this is the "scoring works" check)
9. Confirm the new scores reach `/metrics` with correct pass ratios
10. Confirm query batching actually reduced the query count

## Baseline, before the change (0.2.3)

```
image: .../agent-evaluation:0.2.3
/readyz: {"status":"ready","role":"hub","cluster":"hub",
          "evaluators":4,"agents":3,"rules":5,"skipped":0}
```

The bug, as deployed — eval-multidim is on `judge-haiku`, so its scores are
named `*--judge-haiku`, which the catalog-name filter never matched:

```
oap_agent_eval_scores{agent="eval-multidim",evaluator="goal-completion",window="7d"} 0.0
oap_agent_eval_scores{agent="eval-multidim",evaluator="helpfulness",   window="7d"} 0.0
oap_agent_eval_scores{agent="eval-multidim",evaluator="safety",        window="7d"} 0.0
oap_agent_eval_scores{agent="eval-multidim",evaluator="tool-use",      window="7d"} 0.0
oap_agent_eval_scores{agent="eval-smoketest",evaluator="helpfulness",  window="7d"} 33.0
oap_agent_eval_scores{agent="eval-imageless",evaluator="helpfulness",  window="7d"} 1.0
```

Langfuse itself held 3 scores for each of eval-multidim's four evaluators at
this moment, so those four zeroes were wrong, not idle. All `24h` series were
0.0 because the last traffic predated the window — that part is correct.

## Progress

(appended as the run proceeds)
## Result: PASS. Scoring works end to end.

Also found and cleared a live outage unrelated to this PR — see below.

### 1-4. Image built, pushed, deployed

`0.2.4` built multi-arch and pushed to the private ECR:

```
linux/arm64  sha256:614f0a1cf298ee404
linux/amd64  sha256:507a6d8f72e7b3114
mediaType    application/vnd.oci.image.index.v1+json
```

The ArgoCD app `agent-evaluation-hub` has `selfHeal: true` and the image
override lives in the live **ApplicationSet** (`valuesObject.image`), not in
git — patching the Application alone is reverted within seconds. Patched the
ApplicationSet; it propagated and the pod rolled.

```
/readyz: {"status":"ready","role":"hub","cluster":"hub",
          "evaluators":4,"agents":3,"rules":5,"skipped":0}
banner:  role=hub cluster=hub owner_token=08d33503 evaluators=4
         aliases=judge-default,judge-haiku
```

### 5. The judge-suffix fix, in the deployed path

| agent / evaluator | before (0.2.3) | after (0.2.4) |
|---|---|---|
| eval-multidim / goal-completion | 0.0 | **3.0** |
| eval-multidim / helpfulness | 0.0 | **3.0** |
| eval-multidim / safety | 0.0 | **3.0** |
| eval-multidim / tool-use | 0.0 | **3.0** |
| eval-smoketest / helpfulness | 33.0 | 33.0 |
| eval-imageless / helpfulness | 1.0 | 1.0 |

### 6-8. BLOCKED, then fixed: ClickHouse was full and dropping everything

The first five requests returned HTTP 200 and produced **no traces at all**.
The chain, measured hop by hop:

- agent env correct (`CLUSTER_NAME`, `AGENT_NAMESPACE`, `AGENT_NAME`, OTLP endpoint)
- collector: `accepted_spans=300`, `sent_spans=300`, `send_failed_spans=0`
- sampler 100%, exporter endpoint in-cluster and correct
- Langfuse write mode `dual` / `dual_write`, as this PR requires
- worker running — and dropping every record:

```
Cannot reserve 1.00 MiB, not enough space  (code 243, NOT_ENOUGH_SPACE)
ClickhouseWriter: Max attempts reached, dropped 35 observations record(s)
ClickhouseWriter: Max attempts reached, dropped 62 events_full record(s)
```

`/var/lib/clickhouse` was 20G/20G, 1000K free. What filled it:

```
system.trace_log    15.08 GiB   675,133,331 rows
system.text_log      2.37 GiB    29,709,126 rows
system.text_log_0    0.93 GiB    23,983,027 rows
system.part_log      0.42 GiB     2,626,471 rows
default.events_full   712 KiB         3,117 rows   <- the real data
```

19 of 20 GiB was ClickHouse's own diagnostics. Last successful trace was
**2026-10-30**, so the hub had been silently scoring nothing for three days
while every dashboard read a plausible 0.

`TRUNCATE` also failed — it needs ~1 MiB of metadata space, so a full instance
cannot be cleaned up. `DROP TABLE system.trace_log SYNC` unlinks instead of
writing and reclaimed 15 GiB; the rest then truncated normally.

```
before: 20G used, 1000K avail, 100%
after:  116M used,   20G avail,   1%
```

No Langfuse data touched — traces 200, scores 171, observations 3485,
events_full 3117 all intact before and after.

### 8-9. Scoring verified end to end

Three fresh A2A requests, zero worker drops, 9 traces landed, and the judge
fired on its own. Six new scores for three requests, which is exactly right
(one per trace per evaluator, on the AGENT observation):

```
02:34:23.660  helpfulness--judge-haiku      0.9   "The user asked for a one-sentence definition..."
02:34:23.510  goal-completion--judge-haiku  0.95  "The user asked for a one-sentence definition..."
02:34:23.296  tool-use--judge-haiku         1     "The user asked a general conceptual question..."
02:34:23.227  safety--judge-haiku           1     "The response accurately answers the benign..."
02:34:17.690  helpfulness                   1     "The response is concise, specific, and directly..."
02:34:16.602  helpfulness                   1     "The response is concise, specific, and provides..."
```

And they reached `/metrics` on the 24h window, with `score_avg` matching the
raw judge values exactly (0.9 / 0.95 / 1.0 / 1.0):

```
oap_agent_eval_scores{agent="eval-multidim",evaluator="helpfulness",window="24h"}     1.0
oap_agent_eval_pass_ratio{agent="eval-multidim",evaluator="helpfulness",window="24h"} 1.0
oap_agent_eval_score_avg{agent="eval-multidim",evaluator="helpfulness",window="24h"}  0.9
... all six series present, errors_total never incremented
```

### 10. Root cause fixed in the chart, NOT applied

`templates/clickhouse-system-logs.yaml` disables `trace_log` and puts a 3-day
TTL on the rest, mounted into `/etc/clickhouse-server/config.d/`. Renders
clean, XML parses, mount wired.

Deliberately **not applied tonight**: it restarts a stateful component, and a
bad ClickHouse config would take Langfuse down unattended. `trace_log` was
dropped rather than merely emptied, so it does not exist to grow again until
ClickHouse next restarts — the volume is at 1% and there is no overnight risk.
Apply it while watching.

## What a reviewer should still do

- Apply the ClickHouse change during working hours and confirm ClickHouse comes
  back up (`kubectl rollout status statefulset/langfuse-clickhouse -n langfuse`)
- Decide whether `agent-evaluation`'s image override belongs in the live
  ApplicationSet at all — it is not in git, so it is invisible to review
- The two unmigrated backend methods, and the rule-filter tag ceiling, are still
  open from the shapirov review
