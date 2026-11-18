# Agent Guardrails and Policies — Research Appendix

Supporting research for [`agent-guardrails.md`](agent-guardrails.md). The
design's decisions are stated there and stand without this file. What is here is
the evidence behind three of them, moved out of the design so the decisions stay
short:

| Appendix | Supports |
|---|---|
| [A. Bifrost plugins and images](#a-bifrost-plugins-and-images) | §D1 — why the guard runs at the gateway and not in Bifrost |
| [B. Dogwood](#b-dogwood) | §D6 — why Dogwood is the target shape, offline first |
| [C. Relationship to #152](#c-relationship-to-152) | §3.9 — what this design takes from, and owes, the tool-hop design |

Every source reference is to the upstream file at the version named beside it.
Section references such as §D1 or §3.10 point into the design document.
Revision history is in the PR thread, not here.

---

## A. Bifrost plugins and images

Why the guard belongs at the gateway rather than one hop later, inside Bifrost.

### What Bifrost actually offers

| Fact | Source |
|---|---|
| The only way a plugin can stop a request is `PreLLMHook` returning `*LLMPluginShortCircuit{Error: ...}`. A returned Go `error` is **logged as a warning and the request continues** | `core/schemas/plugin.go:270, 408-429` |
| There is **no content-guardrail plugin in OSS**. The 12 `plugins/` directories are compat, governance, jsonparser, logging, maxim, mocker, modelcatalogresolver, otel, prompts, routing, semanticcache, telemetry | `plugins/`, and the closed `switch` in `transports/bifrost-http/server/plugins.go` |
| Bifrost's own guardrails framework — including its Bedrock Guardrails integration — is **Enterprise**: "Guardrails are an **enterprise-only** feature. They require the enterprise Bifrost image." | `docs/deployment-guides/helm/guardrails.mdx:7-9` |
| The Enterprise framework **fails open**: "Provider error or timeout \| Fails open: the error is reported and the next profile still runs" | `docs/enterprise/guardrails.mdx:557-561` |
| Loading a custom plugin needs a **dynamically linked** binary. "By default, Bifrost builds are **statically linked** … However, statically linked binaries **cannot load Go plugins**." | `docs/plugins/building-dynamic-binary.mdx` |
| A plugin must match the host binary's Go version, libc and **every transitive dependency**, and must be rebuilt on every Bifrost upgrade: "Do I need to rebuild my plugin when upgrading Bifrost? **Yes, absolutely.**" | `docs/plugins/writing-go-plugin.mdx` |
| A typo in a hook symbol **silently disables that hook** rather than failing the load | `docs/plugins/writing-go-plugin.mdx`; `framework/plugins/soloader.go` |

### What OAP actually runs

OAP does not build Bifrost. The `bifrost` chart is an umbrella over the upstream
chart as a vendored dependency, and the image is upstream's public OSS build:

```yaml
# gitops/addons/charts/bifrost/Chart.yaml
dependencies:
  - name: bifrost
    version: 2.1.16
    repository: https://maximhq.github.io/bifrost/helm-charts
# charts/bifrost-2.1.16.tgz → values.yaml
image:
  repository: docker.io/maximhq/bifrost   # OAP pins tag v1.5.0
```

So the two Bifrost-native routes both fail a platform requirement:

- **Enterprise guardrails** need a licensed image from a private registry. OAP is
  an open-source reference platform; a capability that only works for customers
  holding a commercial Bifrost licence is not a platform capability
  (`platform-portability.md`: "Every platform capability is a customer
  capability"). It also fails open by default.
- **A custom `.so` plugin** needs OAP to build, publish and version its own
  dynamically linked Bifrost image, in lockstep with upstream's Go toolchain and
  dependency graph, rebuilt on every upgrade. That replaces "wrap the upstream
  chart" with "own a fork of the gateway", for one feature.


---

## B. Dogwood

### What Dogwood is

| | |
|---|---|
| Repo | `dogwood-policy/dogwood`, Apache-2.0, Rust, created 2026-07-27, latest `v1.1.1` (2026-11-06) |
| Provenance | asserted inside the repo: `NOTICE` reads `Copyright Amazon.com, Inc. or its affiliates`, `SECURITY.md` routes to `aws-security@amazon.com` |
| Relation to Cedar | "an intentional re-implementation of upstream Cedar (the grammar is a faithful translation of `cedar-policy-core` v4.11.0)". Policies **lower to standard Cedar**, temporal and provider terms becoming `context.*` slots filled at runtime |
| Why it matters here | it is the only policy language that can put a content finding, a tool argument and *prior events* in one decision |

### Five corrections to #146, each of which changes something

| #146 says | Verified |
|---|---|
| Operators are `formerly`, `previous`, `since`, `once`, `always` | **Exactly three**: "There are exactly **three** temporal operators … `formerly`, `previous`, and `since`." `once` is a *user-definable macro* (`def temporal once(?w, ?s) { formerly within ?w ?s }`) and an internal grammar rule name; **`always` does not exist** |
| `when guardrails { … }` is a first-class clause | **Transparent sugar**: "`guardrails { E }` is transparent sugar for a bare `when { E }` … The tag is retained only for surface compatibility" |
| The template's `ContentFilterFinding` etc. is "exactly the finding shape Bedrock Guardrails emits" | The types exist in the generated schema but **no policy in the repo uses them** — a code search for `ContentFilterFinding` across `.dw` files returns zero. The exercised pattern is a provider *output record*, and in the OSS corpus it compares an **enum**: `BedrockGuardrails::ContentFilter([…],["VIOLENCE"]).VIOLENCE.confidence in [BedrockGuardrails::Confidence::"MEDIUM", …]` |
| Base actions are `CallTool`, `InvokeLLM`, `InvokeAgent` | **Six, with a hierarchy**: `Mcp` and `Http` are roots; `CallTool` and `UnknownTool` under `Mcp`; `InvokeLLM` and `InvokeAgent` under `Http` |
| Windows like `within 1h` | True but **capped**: four units only (`s`/`m`/`h`/`d`, no week or month) and a schema `max_window` defaulting to **24h**, so `within 48h` is a validation error unless the schema raises it |

The third row closes the loop with D3. The score-shaped surface #146 describes is
the **managed** AgentCore form,
`ContentFilter(categories, contents)[...].confidenceScore.greaterThan(decimal("0.4"))`,
backed by `InvokeGuardrailChecks`. The open-source corpus form takes its
**arguments in the opposite order** and compares an enum. So "Dogwood consumes
Bedrock findings" is true of two different, incompatible spellings, and a policy
cannot move between them untouched.

### Three properties that disqualify the interpreter from a request path

Read in the source, not inferred from the README:

1. **The in-memory temporal engine grows without bound and loses everything on
   restart.** `InMemoryTemporalEngine::observe` is an unconditional
   `trace.points.push(...)`; in partitioned mode it is
   `traces.entry(key).or_default().points.push(...)` over a map whose partitions
   are never reclaimed. No eviction, no size cap, no persistence. A
   `formerly within 1h` **permit** therefore denies everything after a pod
   restart — a control that silently inverts.
2. **Every decision is O(trace length).** `evaluate` re-runs the temporal
   interpreter over the whole trace so far, so unbounded growth and per-request
   replay compound.
3. **Provider execution is unconditional.** "Provider execution is
   **unconditional** … For every decision event, every provider invocation in
   the policy set is evaluated" — not gated by the rule's action, its
   principal/resource constraints, its conditions, or whether it could fire at
   all. On a request path that is every guardrail call in every policy, on every
   request.

Three silent-weakening traps compound it, and each is this platform's named
failure shape:

- A field in the `logged` bag but not `request_context` (or the reverse)
  **silently weakens** either the temporal or the Cedar check.
- A qualified-action-name mismatch makes temporal predicates **silently not
  match** while Cedar still authorizes.
- A `pin` that is not universal and symmetric **silently keeps global
  semantics**: "Nothing warns you — the schema builds and evaluates, just
  without isolation." `pin` is the multi-tenancy lever, so getting it subtly
  wrong means one principal's history authorising another's action.

**One genuinely good finding.** A provider's `implementation` is optional, and a
`ProviderResolver` trait lets the host supply the value from its own code: "the
resolver never touches the Rhai sandbox, so it may do whatever a normal Rust
function can (network calls, model invocations, …)". So an adoption **never has
to ship Rhai**, which removes the `http_get` SSRF class — it "performs **no**
host validation … including internal/link-local addresses (`169.254.169.254`,
`127.0.0.1`, RFC-1918)" — and the script-sandbox question with it.

### Where a production engine could live

**AgentCore Policy is GA and really is Dogwood.** AWS names it outright —
"Dogwood is an open-source policy language … compatible with Cedar" — and
recommends "**Write Dogwood.**" As a service it closes what the interpreter
leaves open: session-scoped durability (24-hour idle timeout), per-caller
isolation ("Two different callers supplying the same session ID get isolated
sessions — the identity is part of the session key"), and service-assigned
timestamps.

**The timeline matters more than the GA badge.** Policy went GA on **2026-03-03**
with Cedar only; Dogwood and temporal policies arrived on **2026-08-06**; Dogwood
itself reached v1.0 on 2026-10-13. So "AgentCore Policy is GA" does **not** mean
temporal policy carries GA-length production history — it is about three months
