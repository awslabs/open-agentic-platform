# Agent Identity & Token Exchange — Architecture Decisions

Status: living document. Captures the significant decisions behind the secretless
workload-identity model and the roadmap to user-delegated (on-behalf-of) access.

Repos / branches:
- OAP (this repo): `aws-samples/sample-open-agentic-platform` — branch `feature/oam-for-agents` (PR #33 → `main`).
- Platform: `aws-samples/appmod-blueprints` — branch `feature/agent-platform-shapirov`.

Reference environment: cluster `peeks-hub`, account `123456789012`, `us-west-2`.
EKS OIDC issuer: `https://oidc.eks.us-west-2.amazonaws.com/id/1BABC5C7BFD3BFE9636A486678E1D6F6`.
Component versions: agentgateway `v1.4.1` (was `v1.1.0`; see ADR-6), Crossplane `v2.2.1`
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

> **Superseded 2026-08-31.** The `actor_token` half of this model is not achievable on
> Keycloak (see the blocker below) and the exchange moved from hop 2 to hop 1. The
> paragraph above is retained as the original record; the design actually being built
> is "Design (2026-08-31) — exchange at hop 1, agent propagates" further down.
> `azp` carries the agent instead of `act`, so the exchange client is per-agent while
> its credential still lives at the gateway rather than in agent pods.

**Standards / grants** (all under agentgateway `backendAuth.oauthTokenExchange`):
- RFC 8693 token exchange (`subject_token`, plus `actorToken`, `resources` per RFC 8707).
- RFC 7523 jwt-bearer / JWT assertion (`assertion`) — matches Keycloak JWT Authorization Grant.
- Entra OBO (jwt-bearer + `requested_token_use=on_behalf_of`).
Multi-hop (agent→agent→MCP) = OAuth Identity & Authorization Chaining / ID-JAG
(token-exchange + jwt-bearer composition; agentgateway `cross_app_access`).

### Gateway blocker — CLOSED 2026-08-21 (agentgateway 1.4.1)

The original blocker was the gateway, and it is resolved. agentgateway was upgraded
v1.1.0 → **v1.4.1** across hub, spoke-dev and spoke-prod (commit `70faa71`).
Verified on each cluster: controller and proxy both v1.4.1, ready, 0 restarts, and
`oauthTokenExchange` present on the `agentgatewaypolicies.agentgateway.dev` CRD.
Live `spec.backend.auth` keys are now `aws, azure, credentials, crossAppAccess, gcp,
key, location, oauthTokenExchange, passthrough, secretRef` (v1.1.0 had only
`aws, azure, gcp, key, passthrough, secretRef`). `oauthTokenExchange` properties:
`actorToken, additionalParams, audiences, backendRef, cache, clientAuth, grantType,
location, path, requestedTokenType, resources, scopes, subjectToken` — only
`backendRef` is required.

Pre-flight before the bump compared the full `traffic` CRD subtree 1.1.0 → 1.4.1:
141 → 312 property paths, **zero removals**, and all eight paths the live
`jwt-auth-policy` depends on still present.

> ⚠️ **Version trap — do not "bump to newest tag".** `cr.agentgateway.dev/charts`
> publishes `v2.2.0`/`v2.2.1`, which are **not** agentgateway releases. Upstream's
> newest final release is **v1.4.1 (2026-07-29)**; there are no 2.x releases. The
> `v2.2.1` chart's `AgentgatewayPolicy` CRD contains **zero** occurrences of
> `oauthTokenExchange`, its `spec.backend.auth` accepts only
> `[aws, key, passthrough, secretRef]` (smaller than v1.1.0), it drops the
> `agentgatewaymodels` CRD, and its CRDs carry 9 `kgateway` references. A numerically
> higher tag is a downgrade here and would silently remove token exchange. Pin is
> recorded in `gitops/addons/bootstrap/default/addons.yaml`.

### Blocker — Keycloak ignores `actor_token` (the delegation half)

**The blocker moved from the gateway to the IdP.** agentgateway 1.4.1 can *send*
`actorToken`; Keycloak 26.3.3 does not *accept* one. Probed directly against the live
realm on 2026-08-25 (scripts kept under `/tmp/tx-probe/`, see "How to re-run" below).

What works:

- Standard Token Exchange V2 is a real RFC 8693 implementation for the
  **internal-internal** case. With `standard.token.exchange.enabled=true` on a
  confidential client, a subject-only exchange returns **HTTP 200** and a valid
  access token. The grant is advertised in realm discovery.
- The per-client toggle is mandatory. Without it every request fails
  `400 invalid_request` / "Standard token exchange is not enabled for the requested
  client", regardless of the other parameters. Client authentication succeeds
  independently of the toggle (`client_auth_method="client-secret"` in the Keycloak
  event log), so a 400 here means the toggle, not the credential.

What does not work, and **fails silently**:

| `actor_token` sent | result |
|---|---|
| real EKS SA token, `actor_token_type=jwt` | HTTP 200, no `act`, no `may_act` |
| real EKS SA token, `actor_token_type=access_token` | HTTP 200, no `act`, no `may_act` |
| garbage string | HTTP 200, no `act`, no `may_act` |
| empty string | HTTP 200, no `act`, no `may_act` |
| subject token reused as actor | HTTP 200, no `act`, no `may_act` |

Every row returns 200 with an **identical claim set**. Keycloak treats `actor_token`
as an unrecognised parameter and neither parses nor validates it. This is worse than
a rejection: implement ADR-6 as originally written and the exchange succeeds, checks
stay green, and the issued token carries the user as `sub` with **no actor at all** —
so you would believe you had a delegation chain and an audit trail showing "user Z via
agent Y", and have neither, with no signal that anything is missing.

Two further results from the same probe:

- **External token as subject is rejected** (cleanly): `400` / "Parameter
  'subject_token' supports access tokens only". So the EKS SA token cannot be the
  subject either. External→internal needs **JWT Authorization Grant** (RFC 7523), which
  is GA from Keycloak 26.6 and listed as supported-and-enabled-by-default in 26.7.2, or
  the deprecated Legacy V1. *Correction: recording this as a dead end was a
  methodological error on my part, not a Keycloak limitation — the probe used the wrong
  grant.*
- **`audience` filters, it cannot add.** `audience=<client>` returns `400` /
  "Requested audience not available: <client>" unless that audience already resolves
  from the requester's client scopes and role mappings. This is a concrete prerequisite
  for the delegation outputs on `gateway-identity` below, not a limitation.

Why upstream nonetheless ships `actorToken`: it is optional in agentgateway's schema
and other IdPs do honour actor semantics (Entra OBO). agentgateway implements the spec
surface and is IdP-agnostic; the gap is specific to the Keycloak + `actorToken`
combination, not a defect in either product.

**Keycloak's own delegation feature is not the mechanism ADR-6 assumed.** Per the
upstream guide, Standard V2 supports only use-case (1) internal-internal, and
"Delegation per RFC 8693" is listed as *experimental* via a separate **Token Exchange
Delegation** feature. That feature emits **`may_act`** (a subject *pre-authorising* a
named actor), not `act` (an attestation that an actor *is* acting); requires the actor
to be a **Keycloak user holding the `impersonation` role** from `realm-management`,
selected via the parameterized scope `delegation:admin`; requires an **interactive user
consent screen every session**; and is flagged "Do not use this feature in production
environments". None of that fits an agent workload as the actor. It is also not enabled
here: the StatefulSet's container args are `["start"]` with no `KC_FEATURES`, and the
feature requires `--features=token-exchange-delegation,parameterized-scopes`.

*Unverified:* the guide read was the 26.7.2 nightly; whether `token-exchange-delegation`
exists at all in our 26.3.3 was not established. It does not change the decision.

#### How to re-run this probe

The realm mutation is a throwaway confidential client, created and deleted by the
script (verified 0 residual matches; the `langfuse` client was never modified):

```bash
# admin password: secret/keycloak-config key KC_BOOTSTRAP_ADMIN_PASSWORD (ns keycloak)
# 1. create client with attributes: {"standard.token.exchange.enabled": "true"},
#    publicClient=false, directAccessGrantsEnabled=true
# 2. password-grant a user token THROUGH that client (azp=<client>) so the
#    "client exchanges its own token" exception applies and no aud wiring is needed
# 3. POST grant_type=urn:ietf:params:oauth:grant-type:token-exchange with
#    subject_token + subject_token_type=...:access_token, plus actor_token
# 4. base64-decode the returned access_token and look for `act` / `may_act`
# 5. delete the client
```

### Design (2026-08-31) — exchange at hop 1, agent propagates

**Decision: split the target.** ADR-6 bundled two independent things: *audience-scoped
exchange* (achievable now) and the *`act` delegation chain* (not achievable on
Keycloak — see the blocker above). Do the first, substitute `azp` for the second.

1. **Upgrade agentgateway.** ✅ **done** — v1.4.1 on all three clusters,
   `oauthTokenExchange` live (commit `70faa71`).
2. **Answer the delegation gate.** ✅ **done** — no `act`. See the Keycloak blocker.
3. **Propagate the caller token in the base image.** ✅ **done** — see below.
4. **Delegation on the `gateway-identity` trait** — designed below, not yet implemented.
   Delivered by extending the existing trait rather than adding a second one: the agent's
   own ServiceAccount identity and the delegated caller identity are two branches of one
   question, and `identity.py:outbound()` already resolves them in one place. Two
   parameters, because they buy different things and stage differently:
   `tokenExchange: true` is the enablement gate and yields **attribution** (`azp` = this
   agent), needing only the agent's own IdP client; adding `delegateTo: [...]` yields
   **containment** (`aud` narrowed to those backends), which additionally requires a
   Keycloak client and client role per named backend. Deliberately not called `audiences`:
   `audience` already means the ServiceAccount token's audience on this trait.
5. **Per-tool authorization at the gateway** — requires `mcpAuth.enabled: true`
   (currently `false` in the agent-gateway chart, so no MCP-level authorization is
   active at all yet). See "Authorization lives at the gateway, not at the exchange"
   for the three CRD-verified constraints that shape it.

#### Where the exchange happens, and why there

**At hop 1, attached to the agent's own HTTPRoute.** Not at hop 2, and not in the
agent process.

```
hop 1   user --[user JWT]--> gateway --[exchanged JWT]--> agent
                             ^ oauthTokenExchange attaches HERE
hop 2   agent --[same JWT, forwarded verbatim]--> gateway --> MCP
```

Two verified facts force this shape.

`clientAuth.clientId` is what Keycloak stamps into `azp` (their docs show
`"azp": "requester-client"` on exchange output). So the exchanging client must be
per-agent, or every agent's token looks identical.

The gateway validates exactly one JWT per request: `traffic.jwtAuthentication` has a
single `location`, and `providers` is a list of issuers for that one token rather than
a list of tokens. So the user and the agent cannot arrive as two separate tokens; both
identities must live in one token, as `sub` and `azp`.

Attaching at hop 1 satisfies both without a sidecar and without exchange code in the
image, because **each agent already has its own HTTPRoute** (`agents/my-agent`,
`default/code-assistant`, `default/oap-assistant-a`, `default/web-browsing-agent`).
The CRD permits it: a backend policy "can target a `Gateway`, `ListenerSet`, `Route`
..., or a `Service` or `Backend`", with precedence
`Gateway < Listener < Route < Route Rule < Backend or Service`.

#### Three modes

| mode | user identity | agent identity | containment |
|---|---|---|---|
| delegated, no trait | `sub` | none (`azp` is the login client) | none, `aud: account` |
| delegated, trait engaged | `sub` | `azp` | `aud` = that agent's permitted backends |
| autonomous (no hop 1) | none | `sub` = `system:serviceaccount:<ns>:<name>` | per the SA token |

Autonomous runs never traverse hop 1, so no exchange occurs and the ServiceAccount
path remains. That is not a fallback to be removed later; it is the design for
scheduled work.

#### What shipped in the base image

`app/identity.py` plus a middleware registration in `app/main.py`. The entire
contract, and the whole of what must be reimplemented in other runtimes:

> Forward the caller's bearer token if one arrived, otherwise use the agent's own
> projected ServiceAccount token.

Deliberately identity-provider agnostic: it inspects no claims and knows nothing about
token exchange, so a customer on Okta or Entra changes trait configuration, not the
image. If a route policy exchanged the token upstream, the agent propagates the result
without knowing that happened.

Two supporting changes were required rather than optional:

- **MCP pools are keyed by credential.** An MCP connection binds its credential when
  it opens, so the previous process-global pool would have run one caller's tool calls
  under whoever's token opened the connection, and served them that caller's filtered
  tool list. That is a cross-caller escalation created by propagation, not a
  pre-existing one.
- **The `/chat` agent cache is keyed by (caller, session).** `contextId` is
  caller-supplied, so keying on it alone let one caller fetch another's agent, whose
  connections carry that caller's credential. Note the A2A JSON-RPC path caches inside
  the Strands SDK and is **not** fixed by this; see open questions.

`PROPAGATE_CALLER_TOKEN=false` restores workload-only identity on every hop.

#### Delegation on the `gateway-identity` trait (design, not implemented)

> **Revised 2026-09-28.** Originally scoped as a separate `delegated-identity` trait.
> Delivered instead by extending the existing `gateway-identity` trait, because the two are
> mechanically disjoint (that trait is `patch:`-only today; delegation is `outputs`-only, and
> a KubeVela template may carry both) and because a developer should answer "what identity
> does my agent have" once. `identity.py:outbound()` already treats the caller token and the
> ServiceAccount token as one decision. Cost of merging: `gateway-identity` is
> `podDisruptive: true`, so enabling delegation restarts the workload even though only a
> gateway-side resource changed.

Named for the capability, not the provider. A trait called `keycloak-token-exchange`
would break OAM portability, which `.kiro/steering/oam-authoring.md` forbids: a
developer's Application must deploy unchanged anywhere.

Emits, for the component it is attached to, and patching the workload not at all:

1. An `AgentgatewayPolicy` targeting the component's existing `HTTPRoute`:

```yaml
targetRefs:
  - group: gateway.networking.k8s.io
    kind: HTTPRoute
    name: <context.name>
backend:
  auth:
    oauthTokenExchange:
      backendRef: { name: <idp token endpoint backend> }
      path: <token endpoint path>          # from global.*, never from the developer
      audiences: <parameter: permitted MCP backends>
      clientAuth:
        clientId: <context.name>
        secretRef: { name: <context.name>-idp }
```

2. An **abstract identity claim** for the IdP client, satisfied by a swappable
   Crossplane composition. This follows ADR-4's `XPodIdentity` arrangement, so the
   Keycloak-specific part is one composition a customer replaces. Client id equals
   `context.name`, preserving ADR-3's single identity anchor across ServiceAccount,
   container, component, and IdP client.

Endpoint and realm come from `global.*` chart values, the way `global.awsRegion`
already does. The developer's Application names no provider and no URL.

**Keycloak-side prerequisites**, both learned from the probe and confirmed in
Keycloak's docs:

- The **subject token must already carry the requester client in `aud`**, or the
  exchange is rejected. Do not solve this with a static audience mapper listing every
  agent. Give each agent client a role (e.g. `use`) and map those roles onto the
  user-facing client so audience resolution populates `aud`.

  > **Revised 2026-09-28.** This bullet previously continued: "assign it to permitted
  > users… This makes the precondition useful: it becomes the enforcement point for
  > *which users may invoke which agent*, checked at the gateway before the agent is
  > reached." That is no longer the design. Assigning a client role per user-tool pair is
  > a manual Keycloak step for every pair, which fails the customer bar in
  > `.kiro/steering/project.md` ("A change is **not done** until it works without human
  > intervention from a clean state"). The role is granted broadly instead, so the
  > exchange always succeeds, and authorization moves to the gateway. See
  > **"Authorization lives at the gateway, not at the exchange"** below.

- **`audiences` filters, it cannot add.** Each MCP backend needs a client scope with
  client role mappings so the requested audience resolves; otherwise Keycloak returns
  "Requested audience not available".
- Standard token exchange enabled on each agent client. The raw Keycloak attribute is
  `standard.token.exchange.enabled: "true"` (verified attribute name), but Crossplane
  `provider-keycloak` v3.1.0 models it as a first-class boolean, so a Composition sets
  `spec.forProvider.standardTokenExchangeEnabled: true` and does NOT need the
  `extraConfig` map.

  > **Corrected 2026-09-30**, after installing the provider and introspecting its CRDs.
  > Earlier notes here and in the implementation plan said `extra_config` was "the escape
  > hatch" for this attribute, on the basis of Terraform provider docs. That was true of
  > older versions; v3.1.0 exposes it natively. Verified shapes, cluster-scoped:
  > `openidclient.keycloak.crossplane.io`, kind `Client`, storage version **v1alpha2**
  > (v1alpha1 still served). Relevant `spec.forProvider` fields: `clientId`, `realmId`,
  > `accessType`, `standardTokenExchangeEnabled` (bool),
  > `allowRefreshTokenInStandardTokenExchange` (`NO` | `SAME_SESSION`, also native, which
  > covers the refresh-token switch noted under Deferred), `serviceAccountsEnabled`,
  > `standardFlowEnabled`, `extraConfig` (map[string]string, still available but not
  > needed here). Note `clientSecretSecretRef` is an INPUT for supplying a known secret;
  > the generated secret comes out via `spec.writeConnectionSecretToRef`.
- Optionally the `downscope-assertion-grant-enforcer` client policy executor, which
  enforces downscoping only.

#### Authorization lives at the gateway, not at the exchange

**Decided 2026-09-28.** The token exchange provides **containment** and **attribution**.
It is not the authorization point.

- *Containment:* `aud` narrows from realm-wide `account` to the agent's declared MCP
  servers, so a leaked or logged token is bounded to those backends. Verified: the probe
  showed `aud=['zz-tx-tool','account']` becoming `aud=zz-tx-tool`, with `account` removed.
- *Attribution:* `azp` carries the exchanging client, so every downstream call names
  which agent acted. This is the only available carrier, because `act` is unachievable on
  Keycloak (see the blocker above).
- *Not authorization:* the Keycloak client role that makes `audience` resolve is granted
  broadly, so the exchange succeeds for any authenticated caller. The access decision is
  made by the gateway.

Authorization is `backend.mcp.authorization` on the MCP backend, which the
`mcp-server` ComponentDefinition already emits from its `authPolicy` parameter
(`platform/oam/definitions/components/mcp-server.cue:265-282`, unused today because the
agent-gateway chart ships `mcpAuth.enabled: false`).

Three constraints, read from the live CRD `agentgatewaypolicies.agentgateway.dev` on
`oap-dev`. Recorded because each one invalidates a design someone will otherwise attempt.

**1. CEL is the only policy language.** `policy.matchExpressions` is "CEL expressions that
must all evaluate to true for the rule to match", each item "A Common Expression Language
(CEL) expression", max 16384 characters, up to 256 expressions. There is no Cedar or Rego
field. Other languages are reachable only by delegating to an external service
(`externalAuthorization`, or `mcpGuardrails` for argument-level decisions), where the
language is whatever that service uses.

**2. Discovery permission is derived from call permission and cannot be set separately.**
The CRD, verbatim:

> List operations, such as `list_tools`, will have each item evaluated. Items that do not
> meet the rule will be filtered. Get or call operations, such as `call_tool`, will
> evaluate the specific item and reject requests that do not meet the rule.

One rule set governs both. So "any identity may list tools, invocation restricted by
policy" is **not expressible as CEL**: a rule that denies calling a tool also hides it from
`tools/list`. `mcp.methodName` exists but is a post-request variable (access logs), not
available during authorization.

This is a deliberate convention, not an agentgateway gap. ToolHive (Cedar) has list
methods "bypass request-level authorization entirely" and filters the response using
`Action::"call_tool"` per item, and ships a `FeatureType` entity with `operation: "list"`
that its docs mark "not currently used for authorization". AWS AgentCore Gateway (Cedar)
states the rule directly: a principal may list a tool only "if there exists any set of
circumstances under which a call to that tool would be permitted". LiteLLM applies one
permission set to "list or call".

The consequence for design: the axis that separates discovery from invocation is
**arguments and tool metadata**, not identity. Identity- or name-based rules necessarily
filter discovery, which is the desired behaviour (an agent that may never call a tool
should not see it). Argument-based rules leave discovery open automatically, and require
`mcpGuardrails`/ExtMCP because `mcp.tool.arguments` is post-request and unavailable to CEL.

**3. `Deny` fails open on evaluation errors.** From the CRD: "`Deny` is not recommended
because expression failures fail to deny; prefer `Allow` or `Require`." `action` is an enum
of `Allow | Deny | Require` defaulting to `Allow`, and "if at least one `Allow` rule is
configured, requests are denied unless at least one allow rule matches". So write the
initial permissive rule as `Require` on `has(jwt.sub)` rather than a bare allow, so later
narrower `Allow` rules compose instead of being bypassed.

`has(jwt.sub)` rather than a `realm_access` check, because the gateway admits two identity
classes: Keycloak user tokens (which carry `realm_access`) and EKS OIDC ServiceAccount
tokens from agents (which do not — see the note at
`gitops/addons/charts/agent-gateway/values.yaml:60`). A `realm_access` rule would admit
humans and exclude every agent. `sub` is present in both.

Where the policy belongs: the agent-gateway chart, beside the existing
`templates/mcp-policy.yaml` (which is authentication only) and attached to the shared
`mcp-servers-backend` AgentgatewayBackend. Backend-scoped rules apply to every target in
that backend, and Solo's guidance is to keep one policy and discriminate with
`mcp.tool.target` rather than write one per server. That also puts the policy in an
operator-owned file rather than in a developer's OAM Application, matching how
`jwt-policy.yaml` and `credential-passthrough-policy.yaml` are already delivered.

**Open before implementing:**

- ~~Whether a Crossplane Keycloak provider is installed, or client provisioning must go
  through a Job like the existing `keycloak-config` one.~~ **Decided 2026-09-28:**
  Crossplane `provider-keycloak`
  (`xpkg.upbound.io/crossplane-contrib/provider-keycloak:v2.7.2`) plus an XRD and
  Composition, following the live `XPodIdentity` precedent. The existing
  `keycloak-client-provisioner` chart cannot serve per-agent clients: it is per-cluster and
  operator-declared in a fleet member file, its `jq`-built client JSON has no `attributes`
  key so it cannot set `standard.token.exchange.enabled` (proven: `agentgateway-dev`, which
  it created, lacks the attribute), it cannot create client roles, it treats HTTP 409 as
  "skip" so it never updates, and it writes to AWS Secrets Manager where
  `clientAuth.secretRef` needs a Kubernetes Secret. It can be installed from OAP rather
  than upstream: OAP already ships its own provider at
  `gitops/addons/charts/crossplane-agentcore/templates/01-provider.yaml`.
- ~~`generate.sh` produces a clean diff. It may not: `agent.yaml` has drifted from
  `agent.cue` and regenerating drops its `opentelemetry-instrument` command (issue #50).~~
  **Stale as of 2026-09-28:** `agent.cue:83` carries
  `command: ["opentelemetry-instrument", "python", "-m", "app.main"]` and so does the
  generated `agent.yaml:79`, one occurrence each on `origin/main`, so regenerating does not
  drop tracing. `us-east-1` no longer appears in `agent.yaml` either. The `agent.yaml`
  entries under "Known violations" in `.kiro/steering/oam-authoring.md` are both out of
  date; `decentralized-observability-identity.yaml` having no CUE source still holds.
  Running `generate.sh` remains unverified, but the two documented reasons to expect a
  dirty diff are gone.
- Whether `client-auth-federated:v1` / `kubernetes-service-accounts:v1` (both GA in
  Keycloak 26.7.2, absent from our 26.3.3) let the agent client authenticate with its
  projected ServiceAccount token instead of a secret. This decides whether
  `clientAuth.secretRef` is needed at all. Not on the critical path, since the
  credential sits at the gateway rather than in agent pods either way.

#### Deferred

- **Per-call audience scoping.** Hop-1 exchange mints one token for the agent's whole
  permitted surface, coarser than a token per backend, though still bounded per agent
  versus today's realm-wide `aud: account`.
- **Token lifetime.** The exchanged token is minted once at hop 1 and capped by the
  subject token's `exp` (~1h). Long-running tasks need a strategy.
- **Multi-hop chaining.** `azp` is overwritten, not nested, so agent→agent→MCP loses
  the middle actor. This is the only thing `act` would buy, and the reason phase 7
  stays parked.


### Open questions — updated

- ~~**Keycloak delegation support:** can our Keycloak issue a genuine RFC 8693
  delegation token with an `act` claim (vs impersonation)?~~ **ANSWERED 2026-08-25: no.**
  `actor_token` is accepted syntactically and ignored semantically (HTTP 200, no `act`,
  even for a garbage or empty actor). Keycloak's only delegation surface is the
  experimental `token-exchange-delegation` feature, which is `may_act`-shaped,
  Keycloak-user-shaped, consent-gated, and not production-supported. See the blocker
  section above for the evidence table.
- **Token lifetime:** user tokens are ~1h; the exchanged token TTL is capped by the
  subject `exp`. Long tasks need a refresh/offline strategy or must be bounded to the
  user session. Sharper now that the exchange happens once at hop 1: an agent holds a
  token minted at request start for the whole task.
- **Not fully secretless:** the exchange client is **per agent** (that is what puts the
  agent in `azp`), but its credential is referenced by the route policy and so lives at
  the **gateway**, never in agent pods. The inbound SA-token path stays secretless.
  Keycloak 26.7's `client-auth-federated` / `kubernetes-service-accounts` may remove the
  secret entirely; unverified, and not on the critical path.
- **New — is `refresh_token` needed from the exchange?** Keycloak gates it behind the
  client's `Allow refresh token in Standard Token Exchange` switch (default `No`).
  Relevant only if an exchanged token must outlive the subject token.
- **New — A2A session cache is not caller-keyed.** `/chat` now caches agents per
  (caller, session), but the A2A JSON-RPC path caches inside the Strands SDK keyed on
  context id alone, which we do not control. A caller supplying another caller's context
  id over A2A can still reach that caller's agent and therefore its MCP connections.

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
| agentgateway v1.4.1 — `backend.auth.oauthTokenExchange` available | done, verified on hub + spoke-dev + spoke-prod |
| Keycloak Standard Token Exchange V2 (internal-internal, subject-only) | **works** — verified HTTP 200 with the per-client toggle on |
| **`act` delegation via `actor_token`** | **not possible on Keycloak 26.3.3 — silently ignored (ADR-6 blocker)** |
| Caller token propagation in `strands-agent-base` (`app/identity.py`) | done — per-credential MCP pools, 32 unit tests |
| Delegation on `gateway-identity` (per-agent IdP client + route policy) | designed, not implemented |
| MCP-level authorization (`mcpAuth.enabled`) | not started — currently `false`, so no MCP authz is active |
