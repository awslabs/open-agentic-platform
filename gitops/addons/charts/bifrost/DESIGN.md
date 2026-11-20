# Bifrost Gateway — Design Notes

## Current state

Bifrost runs with governance enabled but `is_vk_mandatory: false` — all
in-cluster callers can reach the Bedrock backend without authentication.
This is acceptable while agents are trusted workloads on a private cluster,
but does not provide per-workload rate limiting, spend tracking, or revocation.

## TODO: Per-workload Virtual Key minting (separate feature)

### Goal

Every deployed agent gets its own Bifrost Virtual Key (VK) automatically, with
its own budget and rate limit. No operator action required. VKs can be revoked
or rotated per workload without affecting others.

### Target architecture

1. **Bifrost management API** — Bifrost's `POST /api/governance/virtual-keys`
   creates a VK scoped to a provider/model set with a budget and rate-limit
   attached. The response contains the token the caller must present.

2. **KubeVela `agent` ComponentDefinition** — add a workflow step that runs
   before the Rollout is applied:
   - Calls `POST /api/governance/virtual-keys` with the agent name as the VK id.
   - Writes the returned token into a per-agent Kubernetes Secret
     (`<agent-name>-llm-vk` in the agent's namespace).
   - The Rollout mounts that Secret as `LLM_GATEWAY_API_KEY`.

3. **Idempotency** — check `GET /api/governance/virtual-keys/<id>` first;
   create only if absent. On redeploy the existing VK is reused.

4. **Admin credentials** — the management API now requires Basic auth
   (`bifrost.bifrost.authConfig`, with `disableAuthOnInference: true` so agent
   inference stays open). The `bifrost-admin` Secret is created in each cluster by
   the `bifrost-admin-init` PreSync hook (`templates/admin-init.yaml`) with a random
   password, only when the Secret is missing. The credential stays in namespace
   `bifrost`; components that need the API run there.

5. **Re-enable enforcement** — once minting is in place, set
   `plugins.governance.config.is_vk_mandatory: true`.

### Why not pre-seed VKs in Helm values

- `env.*` references are resolved for provider key `value` fields but **not**
  for governance `virtualKeys[].value` — the string is stored verbatim in SQLite.
- Pre-seeding one shared VK in config cannot provide per-workload isolation.
- Config-as-code VKs cannot be rotated or revoked without a chart redeploy.

### Callout for the implementation PR

- The KubeVela `http` workflow step or a sidecar Job can make the management
  API call. The Job approach (shell + wget) is simpler and doesn't require CUE
  HTTP complexity.
- The admin Secret is created by the `bifrost-admin-init` hook, not by bootstrap.
- Budget and rate-limit IDs can be parameterised in the `agent` ComponentDefinition
  so different agent tiers get different limits.
