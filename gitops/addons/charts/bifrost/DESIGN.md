# Bifrost Gateway — Design Notes

## Current state

Per-workload Virtual Key (VK) minting is wired into agent onboarding and virtual
keys are mandatory. The `agent` ComponentDefinition mints a per-agent VK at deploy
time and Bifrost rejects any inference request without one, so the platform
budgets and rate limits below are enforced rather than cosmetic. The management
API (including VK creation) is behind admin authentication.

Config (`gitops/addons/configs/bifrost/values.yaml`, under `bifrost.bifrost`):

- `client.enforceAuthOnInference: true` and `plugins.governance.config.is_vk_mandatory: true`
  — a valid VK is required on every inference call.
- `authConfig.isEnabled: true` with `existingSecret: bifrost-admin` — the dashboard
  and management API require admin credentials.

**Prerequisite (one-time, at bootstrap):** the `bifrost-admin` Secret (keys
`username`, `password`) must exist in the `bifrost` namespace before Bifrost starts,
and the per-agent mint Job reads the same Secret to authenticate. Create it in
`task ...:bootstrap` alongside the other platform credentials (do not commit
credentials to git). If the Secret is absent, admin auth has no credentials and the
mint Job cannot authenticate.

> **Deploy ordering / blast radius.** Because a VK is now mandatory, any agent that
> reaches Bedrock must be onboarded through the `agent` component (which mints one)
> or carry a hand-issued key via `modelConfig.llmGatewayApiKey` with
> `modelConfig.mintVirtualKey: false`. An agent deployed without either gets
> `401 virtual_key_required`. This is intended: no VK, no identity, no access.

## How per-workload VK minting works (implemented)

Every deployed agent gets its own Bifrost VK automatically, with its own budget and
rate limit. No operator action required. VKs can be revoked or rotated per workload
without affecting others.

### Target architecture

1. **Bifrost management API** — Bifrost's `POST /api/governance/virtual-keys`
   creates a VK scoped to a provider/model set with a budget and rate-limit
   attached. The response contains the token the caller must present.

2. **KubeVela `agent` ComponentDefinition** (implemented) — when
   `modelConfig.mintVirtualKey` is true (the default) the component emits a mint
   Job plus a least-privilege ServiceAccount/Role/RoleBinding:
   - The Job calls `POST /api/governance/virtual-keys` with the agent name as the
     VK `name`, scoped to `modelConfig.vkProvider`/`vkAllowedModels`, with the
     budget and rate limit from the `vk*` parameters.
   - It writes the returned token into `<agent-name>-llm-vk` in the agent's
     namespace via `kubectl apply` (server-side create-or-update).
   - The Rollout mounts that Secret as `LLM_GATEWAY_API_KEY`, `optional: true`, so
     the pod still starts before the Job has run and picks the VK up on its next
     restart.

3. **Idempotency** (implemented) — the Job lists VKs and reuses the token of the
   one already named after the agent, creating a new VK only when absent. Bifrost
   generates the id and token, so idempotency is keyed on the VK **name**, not a
   caller-supplied id. On redeploy the existing VK is reused.

4. **Admin credentials** (implemented) — the management API is behind admin auth
   (`bifrost.bifrost.authConfig`, `isEnabled: true`, `existingSecret: bifrost-admin`).
   The mint Job authenticates with the same `bifrost-admin` Secret. Create that
   Secret once at bootstrap (not per-workload); see Current state.

5. **Enforcement** (implemented) — `client.enforceAuthOnInference: true` and
   `plugins.governance.config.is_vk_mandatory: true` are both set, so a VK is
   mandatory on every inference request.

### Why not pre-seed VKs in Helm values

- `env.*` references are resolved for provider key `value` fields but **not**
  for governance `virtualKeys[].value` — the string is stored verbatim in SQLite.
- Pre-seeding one shared VK in config cannot provide per-workload isolation.
- Config-as-code VKs cannot be rotated or revoked without a chart redeploy.

### Implementation notes

- The mint step is a sidecar-style Job (shell + `wget`), not a KubeVela `http`
  workflow step — simpler, and no CUE HTTP complexity.
- The `bifrost-admin` Secret must be created by the bootstrap task (one-time, same
  pattern as other platform bootstrap credentials). It is not committed to git.
- Budget and rate limits are parameterised per agent via the `modelConfig.vk*`
  fields, so different agent tiers can get different limits.

### Not yet done — Bedrock Guardrails

Content policy (an AWS Bedrock Guardrail plus its Bifrost `guardrails` wiring) is a
separate follow-up. The vendored chart supports it under
`bifrost.bifrost.guardrails` (a `providers` entry with `provider_name: bedrock` and
a `rules` entry referencing it), and the guardrail resource itself is created in AWS
(Crossplane). Until that lands, budgets and model restrictions are enforced but there
is no gateway-layer content filtering.
