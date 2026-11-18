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
old.

**And it cannot be called as an authorization API.** Both surfaces were
enumerated: the control plane has only `CreatePolicy` / `CreatePolicyEngine` /
`UpdatePolicy` and their reads, and the data plane has no `Authorize`. There *are*
`AuthorizeAction` and `PartiallyAuthorizeActions`, but they appear under
"Permission-only actions" and are granted **to the Gateway execution role** — the
service invokes them on your behalf. The capability is real; the wire protocol is
private. A decision is reachable only by putting an **AgentCore Gateway on the
data path**. AWS states the Kubernetes case explicitly: a request through "a
Kubernetes cluster … must forward the WAT header, and you must add custom logic
to propagate the WAT through those hops."

Four further limits bear on whether that is attractive:

| Limit | Consequence for OAP |
|---|---|
| Policy evaluation is **MCP-tools-only** — "the gateway always allows MCP prompts … and resources" | Prompts and resources stay ungoverned |
| "**Gateway application logs don't include policy decisions.**" Decisions appear only as CloudWatch metrics and as spans that require traces explicitly enabled | Per-decision audit is opt-in, not a property of turning policy on |
| **400 KB** Cedar schema per engine, auto-generated from every tool across every attached gateway, non-adjustable | This, not policy count, is the sharding wall for a multi-team platform. AWS's own guidance: "consider using separate policy engines for different gateways" |
| **20** temporal policies per engine, **3** temporal operators per policy, **24h** max window, all non-adjustable | A fleet-wide temporal policy set is small by construction |

So the option set is binary, and worth stating plainly:

| Option | Assessment |
|---|---|
| **Offline: author policies, replay recorded traffic** (**chosen, Phase 3**) | Exactly what the reference interpreter is for, zero production risk, and it answers "does this language express the rules we want" before anyone builds an engine |
| Route OAP's MCP traffic through an AgentCore Gateway | The only route to managed Dogwood enforcement. A real option later, but it moves the tool hop off `agent-gateway` onto an AWS service — a platform-shape decision far beyond guardrails — and still leaves every `centralized` agent's **model** call untouched |
| A Dogwood authorization provider in agentgateway | Best long-term shape, since `backend.mcp.authorization` already takes an `action` + expression form. Needs upstream work OAP does not control |
| Build a durable temporal engine | `TemporalEngine` is the seam, and the crate ships its regression corpus behind a `corpus` feature specifically "so that an alternative implementation of the temporal engine can be checked against the same cases". Possible, and far more than this capability needs |
| Ship the reference interpreter on the request path | **No.** See the three properties above |

### What Phase 3 does, and the one thing that gates it

1. Generate the action schema from OAP's real MCP servers: `dogwood schema mcp
   --manifest tools.json` takes an MCP `tools/list` payload and emits one Cedar
   action per tool, mapping `inputSchema.properties` to `context.input`.
2. Write three policies — one stateless (reproducing something `authPolicy` does
   today, to prove no loss of expressiveness), one `formerly`-based, one
   `count`-based.
3. Replay them over recorded traffic and report which rules the language
   expressed, how often each would have denied real traffic, and a
   false-positive read on the denials.

**The gate is the trace format, and it is a converter rather than a mapping.**
`dogwood replay` consumes a bespoke text format, one line per timepoint:

```text
@0 scope(principal: Drupe::OAuthUser::"alice", resource: Drupe::Gateway::"gw1") request_context(input: { shares: 5 }) Drupe::Action::"ApproveSale"::request(input: { shares: 5 }, callerPrincipal: Drupe::OAuthUser::"alice", requestId: "u1")
```

There is no JSON, NDJSON or OTLP ingestion anywhere in the crate. Converting
OAP's traces means, per event:

| Requirement | Why it is work, not mapping |
|---|---|
| `i64` **seconds**, **strictly increasing** | OTel spans are nanosecond and overlap. "Timestamps measure temporal windows; they do not establish trace order" — trace *position* is the order, so the converter assigns timestamps rather than reusing span times |
| An explicit `::request` / `::response` kind | One tool-call span becomes **two or more** lines: "its eventual `response` or `error` is a separate event" |
| Fields in **both** bags | A field a policy reads *and* correlates on must appear in the trailing logged group **and** `request_context`, or the check silently weakens |
| Fully-qualified quoted action ids | A mismatch makes temporal predicates silently not match |

Whether OAP's Langfuse observations carry tool-call arguments and timestamps at
all is the first thing to check, and it stays an open question (Q4).

---

## C. Relationship to #152

### What this design takes from #152

Three things, already folded in above rather than left as future work:

| Taken | Where it landed |
|---|---|
| `FailClosed` as the default of the nearest adjacent implementation | §D4 — it turns "fail closed" from an argument into a convention already in use in OAP's own gateway |
| Decision provenance: a verdict records the configuration that produced it | §D8 — `profileVersion` + `guardrailVersion`, and the skew metric |
| The tool-call content gap has a hop that can see it | §D7, and the Scope list — it stops being an unanswered hole and becomes a boundary |

### What this design owes #152, and one trap it should inherit

- **The verdict seam is per-hop, not shared.** §D4's guard entries are the content
  verdict at the model hop. #152's engine is the authorization decision at the
  tool hop. Sharing one engine across both sounds tidy and is wrong: the inputs,
  the latency budget and the failure blast radius all differ. What they should
  share is the *shape* — one place, fail-closed, provenance recorded.
- **`redact` is a mutation, and mutations have an ordering trap.** #152 records
  that a request-phase processor may rewrite `params` while CEL RBAC still checks
  the **pre-mutation** values, so an authorization processor must run after every
  mutating one. The wire format states it outright: `McpRequestResult.mutated`
  "replaces JSON-RPC `params` before forwarding to the upstream MCP server" and
  "**the gateway does not re-run other RBAC on the mutated request**". So a
  mutating processor is not merely ordered before authorization — it can hand
  the upstream arguments that no policy ever saw. This design has the same
  hazard in miniature: a `redact` verdict
  changes the prompt that Bifrost then sees. Today nothing downstream authorizes
  on prompt content, so the trap is latent — but anything added that *does* must
  run after redaction, and §D8's record is what makes the before/after visible.
- **Rate and frequency belong to neither engine.** #152 is right that "no more
  than N calls" is not an authorization question; agentgateway answers it
  natively at `spec.traffic.rateLimit`, and Bifrost answers the model-side
  equivalent through `governance` once per-workload virtual keys exist (§2.3).
  Neither this guard nor a policy engine should be made to count.

### Sequencing

They are independent and can land in either order. One soft dependency —
#152's blocking task 2, whether caller identity actually reaches an
ExtMCP processor — **resolves in #152's favour**, and §3.10 shows the evidence.
The prose documents only "selected request headers", but `ext_mcp.proto` gives
`McpRequest` a `metadata_context` field carrying "CEL-evaluated context from
gateway config, one field per config key": the gateway evaluates `jwt.sub`
itself and ships the result. Identity arrives as data, not as a header that has
to survive filtering. #152 can close that task on the wire format.

---

## D. How guardrails are done in practice


The industry has converged on a shape, and it is worth stating because this
design is an instance of it rather than an invention.

**The enforcement point is a gateway, and every serious implementation is
pre/post around the model call.** Bifrost, LiteLLM, Portkey, Kong AI Gateway and
Cloudflare AI Gateway all expose the same two hooks — inspect the request before
the provider, inspect the response after — and all of them treat the detector as
a pluggable backend rather than something they implement. That is exactly the
seam this design uses; the only question OAP has to answer differently is *how*
to get a hook into its gateway (§D1).

**The detector is almost always a managed service.** The four that matter:

| | Bedrock Guardrails | Azure AI Content Safety | Google Model Armor |
|---|---|---|---|
| Standalone callable | yes (`ApplyGuardrail`) | yes — and *only* standalone; the integrated path needs an Azure-hosted model | yes (`:sanitizeUserPrompt`) |
| Injection detection | yes (`PROMPT_ATTACK`) | yes (Prompt Shields, GA Aug 2024) | yes (`pi_and_jailbreak`) |
| PII | yes, detect **and** mask | **not in this service** — PII is Azure AI Language, a second service at ~2.7× the unit price | yes, incl. de-identification |
| Natural-language denied topics | yes — `definition` + up to 5 `examples` | blocklists and trainable custom categories (preview, retiring 2027-04-30) | no |
| Enforces, or only detects | **enforces** (blocks, masks) | detects — "the API doesn't itself block or route content" | detects — the caller "is responsible for blocking" |
| Throughput | 100 requests/s (25 in smaller Regions), but text units per second bind first (design §5) | 100 rps on S0, raised only by emailing support | **1,200 QPM** per project |

Two things follow. Bedrock is the only one of the three that both **enforces**
and takes **natural-language topic definitions**, which is why it is the Phase 1
backend for a Bedrock-based platform. And all three are detectors — the gateway
is the policy enforcement point in every case, so the AI route in §D1 is doing the
job these services assume someone is doing.

**In-process libraries are the common mistake.** NeMo Guardrails, Guardrails AI,
LlamaFirewall and Presidio are all good at detection and all live in the
application image, which is precisely what makes them a convention rather than a
control. The pattern that works is to put them *behind* a gateway hook, where the
platform decides whether they ran.

