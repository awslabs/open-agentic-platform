# Agent Identity & Token Exchange — Architecture Decisions

Status: living document. Captures the significant decisions behind the secretless
workload-identity model and the roadmap to user-delegated (on-behalf-of) access.

Repos / branches:
- OAP (this repo): `aws-samples/sample-open-agentic-platform` — branch `feature/oam-for-agents` (PR #33 → `main`).
- Platform: `aws-samples/appmod-blueprints` — branch `feature/agent-platform-shapirov`.

Reference environment: cluster `peeks-hub`, account `929819487611`, `us-west-2`.
EKS OIDC issuer: `https://oidc.eks.us-west-2.amazonaws.com/id/1BABC5C7BFD3BFE9636A486678E1D6F6`.
Component versions: agentgateway `v1.1.0`, Crossplane `v2.2.1`
(functions: environment-configs v0.3.0, patch-and-transform v0.10.0, cel-filter v0.2.0),
Keycloak `26.3.3`, Bifrost `2.1.16`, vela CLI `1.10.7`.

---

## Goal

Every agent gets a **secretless identity**. Two trust domains:
1. **AgentGateway / MCP** — so agents reach MCP servers and other agents.
2. **AWS** — so agents call AWS APIs (Bedrock, AgentCore memory, etc.).

North star: when an agent calls MCP / another agent **on behalf of a user**, the
downstream request must carry the **user as subject** and the **agent as actor**
(delegation) — for (a) audit logs that show "API call X executed by agent Y on
behalf of user Z", and (b) authorization decisions based on the invoking
user/group/role, not just the agent's blanket workload trust.

---

## ADR-1 — LLM gateway is Bifrost (context)

Migrated LiteLLM → Bifrost. Bedrock via EKS Pod Identity; model alias
`claude-sonnet`. Agent uses Strands `OpenAIModel` against Bifrost `/v1`. Governance
VK auth currently disabled (`enforceAuthOnInference: false`). Not identity-critical
but sets the "gateway is the boundary" pattern.

## ADR-2 — Inbound workload identity = "Shape A" (gateway trusts the cluster EKS OIDC)

**Decision.** Agents authenticate to AgentGateway with their **projected Kubernetes
ServiceAccount token** (audience `agentgateway`). AgentGateway validates it against
the **cluster's own EKS OIDC issuer**, added as a *second* JWT provider on the
gateway policy (alongside the Keycloak provider used for humans).

**Alternatives rejected:**
- *Keycloak-issued workload token (inbound "Shape B")* — Keycloak's external-token
  grants (JWT Authorization Grant / legacy token exchange) require a **confidential
  client secret** and a **per-workload linked Keycloak user**; not secretless, heavy.
- *client_credentials with a per-workload secret* — a secret per workload; rejected.
- *SPIRE* — heavier infra; kept as a fallback if the SA-token path proves insufficient.

**Rationale.** No secret; kubelet auto-rotates the token; per-workload identity is
free (`sub = system:serviceaccount:<ns>:<sa>`); **environment isolation is free**
(dev/prod clusters have distinct OIDC issuers, so a dev token is cryptographically
invalid at the prod gateway). Keycloak remains the **human** IdP unchanged.

**Consequences / how.**
- Gateway `AgentgatewayPolicy.traffic.jwtAuthentication.providers` gets a 2nd
  provider (issuer = cluster OIDC, JWKS over a static `AgentgatewayBackend`
  host `oidc.eks.<region>.amazonaws.com:443` with inline `policies.tls: {}`).
- Authz CEL allows both shapes:
  `(has(jwt.realm_access) && jwt.realm_access.roles.exists(r, r=="default-roles-platform")) || jwt.sub.startsWith("system:serviceaccount:")`.
- The agent app reads the token from `WORKLOAD_TOKEN_PATH` and sends
  `Authorization: Bearer` to MCP (via `streamablehttp_client(headers=...)`).

**Verified:** agent pod → gateway → `mcp-time` returns tools (HTTP 200; was 401
before the provider + agent-code wiring).

## ADR-3 — Identity is composed via OAM traits; the agent owns its ServiceAccount

**Decision.** Identity is modeled as **KubeVela traits** attached to a component,
not baked into each ComponentDefinition:
- `gateway-identity` — projects the `agentgateway`-audience SA token + sets `WORKLOAD_TOKEN_PATH`.
- `aws-service-identity` — grants AWS IAM identity (see ADR-4).

**Key constraint that drove the design:** a **trait can only read `context.name`**
(the component name) — it *cannot* read a component's `parameter.name`. So anything
a trait must reference (the ServiceAccount, the container) has to be named
`context.name`. Therefore:
- The `agent` component was refactored to key **everything** off `context.name` /
  `context.namespace` (Rollout, Services, container, SA, `AGENT_NAME`, gateway route)
  and to **own a dedicated ServiceAccount** = `context.name`. The `name`,
  `namespace`, and `serviceAccount` parameters were **removed** (breaking change:
  OAM Applications drop `properties.name/namespace`).
- Cloud-agnostic naming: `aws-service-identity` (future `gcp-service-identity`, …).
- Added a generic `service-rollout` component (Argo Rollout + health gating, owns
  its SA) as the base for non-agent workloads. `appmod-service`/`dp-service-account`
  kept for compatibility.

## ADR-4 — AWS identity = Pod Identity, "Option C" (self-inject + init-wait) via an XPodIdentity Composition fed by `env-config`

**Decision.** `aws-service-identity` emits a **`PodIdentity` claim**
(`platform.gitops.io`, appmod XRD/Composition). The **`XPodIdentity` Composition**
resolves `clusterName`/`region` from the ambient **`env-config` EnvironmentConfig**
(via `function-environment-configs`) and creates the IAM Role
(`<serviceAccount>-role`) + `PodIdentityAssociation`. The developer passes **no
cluster parameters** — only optional `accessFor` (sibling component policies).

**Why "Option C" (self-inject + init-wait).** EKS Pod Identity injects creds via a
**mutating webhook at pod admission**, which only fires if the association already
exists → a race that broke the pure-trait approach historically. Option C makes the
pod self-inject the creds env (`AWS_CONTAINER_CREDENTIALS_FULL_URI`) + the
`pods.eks.amazonaws.com` projected token, and adds a `wait-for-aws-identity` init
container that blocks until `aws sts get-caller-identity` succeeds. **Verified on
cluster:** the EKS webhook *skips* injection when the creds env is already present
(no duplicate-volume conflict), and STS resolves without a region env.

**`env-config` is the ambient metadata contract.** A cluster-scoped Crossplane
`EnvironmentConfig` named `env-config` on every cluster, carrying at least
`clusterName`, `region`, `vpcId`, `privateSubnetIds`, `publicSubnetIds`. Only
Compositions can consume it (not KubeVela, not raw MRs) — hence the `XPodIdentity`
Composition indirection.

**Verified:** `PodIdentity` claim → Role + `PodIdentityAssociation` Ready
(`clusterName=peeks-hub` from `env-config`); agent pod init-wait logs "AWS identity
ready"; in-pod STS returns the assumed role.

## ADR-5 — Per-cluster EKS OIDC issuer surfaced as the `eks_oidc_provider` annotation

**Decision.** The gateway's workload JWT provider is templated per-cluster from an
`eks_oidc_provider` cluster-secret annotation:
- **Spokes:** the `platform-cluster` Crossplane Composition writes it from
  `status.oidcIssuer` (same Observe+Update `Object` pattern as `aws_vpc_id`);
  automatic for every spoke at provision time.
- **Hub:** set by the OAP `Taskfile` (`agentic:hub-oidc-annotation`), because the
  hub is bootstrapped from a kind cluster before the platform exists (chicken-and-egg).

## Key gotchas (do not re-discover)

- **Crossplane ProviderConfig is `default`**, not `provider-aws-config` (the latter
  is stale in `dp-service-account`). Applies to the classic `*.aws.upbound.io`
  provider. The namespaced `*.aws.m.upbound.io` family has **no** ProviderConfig on
  this cluster — so use the **classic** provider (this is why `agentcore-memory` was
  switched from `bedrockagentcore.aws.m.upbound.io` → `bedrockagentcore.aws.upbound.io`
  + `default`).
- **Keycloak 26.3.3 token-exchange matrix:** Standard Token Exchange v2 (GA) is
  internal-internal only (subject must be a Keycloak access token). External-token →
  Keycloak token needs Legacy Token Exchange V1 (preview, deprecated) or JWT
  Authorization Grant (26.5 preview) — both require a **confidential client** and a
  **linked user**. This is why inbound identity is Shape A, not Keycloak-brokered.
- ArgoCD stuck sync operations pin an old git revision; terminate with
  `kubectl patch app <n> -n argocd --type merge -p '{"operation":null}'` then refresh.
- OAM defs must be lowercase-hyphen (RFC-1123). Regenerate with
  `bash platform/oam/generate.sh` after editing CUE; commit both `.cue` and generated YAML.

---

# Roadmap — User-delegated access via gateway token exchange

Target = ADR-6 (below). This is the mechanism that "checks the security boxes":
per-call audit of *user + agent*, and user/group/role-based authorization on MCP/API
calls.

## ADR-6 (TARGET) — User on-behalf-of via AgentGateway backend token exchange

**Model.** Gateway-side **backend** auth exchanges the inbound bearer for a
downstream token before calling the upstream, using **RFC 8693 delegation**:
`subject_token = user token`, `actor_token = agent's SA token` → downstream token
with `sub = user`, `act = agent`, audience-scoped to the target MCP/agent. Secrets
(the exchange client credential) live at the **gateway** (a k8s Secret), never in
the agent. Our existing pieces compose: **Shape A SA token = the `actor`**, the
**Keycloak user token = the `subject`**.

**Standards / grants** (all under agentgateway `backendAuth.oauthTokenExchange`):
- RFC 8693 token exchange (`subject_token`, plus `actorToken`, `resources` per RFC 8707).
- RFC 7523 jwt-bearer / JWT assertion (`assertion`) — matches Keycloak JWT Authorization Grant.
- Entra OBO (jwt-bearer + `requested_token_use=on_behalf_of`).
Multi-hop (agent→agent→MCP) = OAuth Identity & Authorization Chaining / ID-JAG
(token-exchange + jwt-bearer composition; agentgateway `cross_app_access`).

### Blocker — pending agentgateway release

- **Feature status in agentgateway:** MERGED to `main`. Data plane in
  `crates/agentgateway/src/http/auth/oauth/` (mod/transport/cross_app_access),
  controller `backend_policies.go` + `agentgateway_policy_types.go`, e2e tests, and
  `examples/traffic-token-exchange/{oauth-rfc8693,jwt-authz-grant}`. PRs **#2189**
  (data plane) and **#2458** (controller). Blog: agentgateway.dev/blog/2026-07-12-…-token-exchange-jwt-assertion-entra-obo.
- **Installed version:** agentgateway **v1.1.0** (proxy + controller). Its
  `AgentgatewayPolicy` `spec.backend.auth` keys are
  `[aws, azure, gcp, key, passthrough, secretRef]` — **no `oauthTokenExchange`**.
- **Therefore:** token exchange is NOT usable on our cluster yet. It requires
  upgrading agentgateway to the release that ships #2189/#2458 (post-v1.1.0; as of
  2026-07-13 appears to still be `main`/pre-release — verify a tagged release before
  the bump).

### Phased plan

1. **Track & upgrade agentgateway.** Wait for / pin the release exposing
   `backend.auth.oauthTokenExchange`; bump the `agentgateway`/`agentgateway-crds`
   addons; re-verify the CRD has the field.
2. **Propagate user identity through the agent (the main app change).** Today the
   agent calls MCP with its *own* SA token. For OBO it must capture the inbound
   **user** token (A2A auth passthrough) and forward it as the subject on outbound
   MCP calls; the SA token becomes the actor. This is the largest new piece.
3. **Attach a backend exchange policy** (`AgentgatewayPolicy` with
   `backend.auth.oauthTokenExchange`) in front of the MCP backends: `subject_token`
   = user token, `actorToken` = agent SA token, audience-scoped per tool; token
   endpoint as an `AgentgatewayBackend`, client secret from a k8s Secret.
4. **Authorization at the gateway.** Enforce user/group/role (from the user's
   claims) as the chokepoint — a tool call is allowed only if the invoking user is
   authorized, not merely because the agent workload is trusted.
5. **Multi-hop chaining (ID-JAG)** for agent→agent→MCP, preserving the original user
   and accumulating the actor chain so the audit trail stays intact.

### Open questions to resolve during the spike

- **Keycloak delegation support:** can our Keycloak issue a genuine RFC 8693
  delegation token with an `act` claim (vs impersonation)? Historically Keycloak is
  strongest on internal-internal + impersonation; verify before committing. (May
  require Keycloak 26.5 JWT Authorization Grant / config, or gateway-constructed
  delegation.)
- **Token lifetime:** user tokens are ~1h; the exchanged token TTL is capped by the
  subject `exp`. Long autonomous tasks need a refresh/offline strategy or must be
  bounded to the user session.
- **Not fully secretless:** the exchange requires a client secret, but at the
  **gateway** (correct place), not per-agent; the inbound SA-token path stays secretless.

---

## Current state (implemented + verified)

| Area | State |
|---|---|
| Bifrost LLM gateway (Bedrock, claude-sonnet) | done, verified |
| Gateway identity (Shape A) — SA token validated by cluster EKS OIDC | done, verified (agent→gateway→mcp-time 200) |
| `gateway-identity` trait + agent reads `WORKLOAD_TOKEN_PATH` | done, verified |
| `aws-service-identity` trait → `XPodIdentity` → Role + PodIdentityAssociation | done, verified (STS in-pod) |
| `env-config` EnvironmentConfig (clusterName/region) | live on hub |
| `service-rollout` component; `agent` refactored to `context.name` + owns SA | done |
| `agentcore-memory` classic provider + `default` | done, memory provisions |
| Per-cluster `eks_oidc_provider` (spoke Composition; hub Taskfile) | done |
| **User-delegated token exchange (OBO)** | **blocked on agentgateway release (ADR-6)** |
