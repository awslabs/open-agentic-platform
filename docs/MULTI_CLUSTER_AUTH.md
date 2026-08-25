# Multi-Cluster Authentication — Agentic Platform Integration

## Status

**Proposed** — Phase 1 implementation pending.

This document describes how the Open Agentic Platform consumes the
multi-cluster authentication mechanism provided by the underlying
Platform Engineering on EKS solution. It is a use-case-specific
integration guide; the generic mechanism, contract, and design
trade-offs live in the platform doc.

> **Source of truth for the mechanism**:
> [Multi-Cluster Authentication (appmod-blueprints)][platform-doc].
>
> [platform-doc]: https://github.com/aws-samples/appmod-blueprints/blob/main/docs/platform/MULTI_CLUSTER_AUTH.md

If you have not read the platform doc yet, start there. This doc
assumes familiarity with the OIDC Provider Abstraction, the
Secrets Manager contract, and the per-environment client provisioning
model.

## Context

The agentic platform deploys components across hub and spoke clusters:

- **Hub** — runs the IdP (Keycloak in the reference impl), Backstage,
  Crossplane, and shared agentic services like `langfuse` and
  `bifrost`.
- **Spokes** — run `agentgateway` (the entry point for agent traffic)
  and developer-deployed agent workloads.

Both `agentgateway` and the agents themselves authenticate against the
platform IdP. They consume per-environment OAuth clients provisioned
by the platform's reference (or customer-supplied) provisioner.

Agents are **not** preinstalled by the platform. Developers deploy
them via the platform's OAM (Open Application Model) abstraction,
backed by Argo Rollouts for progressive delivery. The default agent
runtime image is built on AWS Strands SDK with MCP tooling injection
and AgentCore memory; customers can extend it or **bring their own
image (BYOI)** conforming to the same runtime contract.

## What the agentic platform requires from the platform

Per environment (`hub`, `dev`, `prod`):

| Requirement | Provided by platform mechanism |
|---|---|
| Per-env DNS hostname (`<env>.peeks.dev.<base-domain>`) | ExternalDNS + ACM cert |
| OAuth client `agentgateway-<env>` (resource server) | Provisioner Job |
| OAuth client `agent-runtime-<env>` (M2M + token-exchange) | Provisioner Job |
| OAuth client `ui-<env>` (public + PKCE; if a UI is deployed in the env) | Provisioner Job |
| Contract entries in Secrets Manager at `peeks/<env>/oidc/*-client` | Provisioner Job |
| ExternalSecrets infrastructure on each cluster | Platform addon |

The agentic platform repo declares these clients to the provisioner
via a values fragment and consumes the resulting Secrets Manager
entries via ExternalSecrets in each cluster's overlay.

## Client declarations

The agentic platform contributes the following client declarations to
the provisioner (D4 in the platform doc):

```yaml
# open-agentic-platform/gitops/addons/oidc-clients/values.yaml
clients:
  - name: agentgateway
    env: hub
    type: confidential
    flows: [resource_server]
    audience: agentgateway-hub
    consumerSecretPath: peeks/hub/oidc/agentgateway-client

  - name: agentgateway
    env: dev
    type: confidential
    flows: [resource_server]
    audience: agentgateway-dev
    consumerSecretPath: peeks/dev/oidc/agentgateway-client

  - name: agentgateway
    env: prod
    type: confidential
    flows: [resource_server]
    audience: agentgateway-prod
    consumerSecretPath: peeks/prod/oidc/agentgateway-client

  - name: agent-runtime
    env: dev
    type: confidential
    flows: [client_credentials, token_exchange]
    consumerSecretPath: peeks/dev/oidc/agent-runtime-client

  - name: agent-runtime
    env: prod
    type: confidential
    flows: [client_credentials, token_exchange]
    consumerSecretPath: peeks/prod/oidc/agent-runtime-client
```

These declarations are surfaced to the platform provisioner through
the agentic platform's fleet overlay, so they get materialized when
the platform syncs.

## Component integration

### agentgateway

Each cluster's `agentgateway` deployment validates JWTs issued by the
platform IdP and authorizes requests using a configurable group claim.

**Chart values** (parameterized; no IdP-specific URLs hard-coded):

```yaml
# open-agentic-platform/gitops/addons/agentgateway/values.yaml
ingress:
  hostname: "{{ .Values.env }}.peeks.dev.{{ .Values.baseDomain }}"
  tlsCertificateArn: "{{ .Values.tlsCertificateArn }}"

oidc:
  # Read from K8s Secret materialized by ExternalSecrets
  secretName: agentgateway-oidc-client
  groupsClaim: "{{ .Values.groupsClaim | default \"groups\" }}"

authorization:
  requiredGroups:
    - admin
```

**JWT policy** is templated against the Secret keys. Snippet:

```yaml
# templates/jwt-auth-policy.yaml
apiVersion: agentgateway.dev/v1alpha1
kind: AgentgatewayPolicy
metadata:
  name: jwt-auth-policy
spec:
  targetRefs:
    - group: gateway.networking.k8s.io
      kind: Gateway
      name: agentgateway-proxy
  traffic:
    jwtAuthentication:
      mode: Strict
      providers:
        - issuer: <pulled from Secret key "issuer">
          audiences:
            - <pulled from Secret key "audience">
          jwks:
            remote:
              uri: <pulled from Secret key "jwks_uri">
              cacheDuration: 5m
    authorization:
      action: Allow
      policy:
        matchExpressions:
          - jwt.<groupsClaim>.exists(g, g == "admin")
```

The policy uses the public JWKS URL over HTTPS. No in-cluster Service
ref to the IdP — that was the source of the original spoke breakage.

### Agent runtime — OAM `oidc-identity` trait

Agents deployed via OAM apply the `oidc-identity` trait to inherit
platform-managed identity:

```yaml
# Example agent deployment authored by an application developer
apiVersion: core.oam.dev/v1beta1
kind: Application
metadata:
  name: support-bot
  namespace: my-team
spec:
  components:
    - name: support-bot
      type: agent
      properties:
        image: my-registry/support-bot:1.2.3   # BYOI
        replicas: 2
      traits:
        - type: oidc-identity
          properties:
            agentId: support-bot
        - type: mcp-tooling
          properties:
            servers: [k8s-tools, slack-tools]
        - type: agentcore-memory
        # Argo Rollouts strategy
        - type: rollout
          properties:
            strategy: canary
            steps: [25, 50, 100]
```

The `oidc-identity` trait wires up:

1. The shared `agent-runtime-<env>` client credentials, mounted from
   the K8s Secret produced by ExternalSecrets.
2. **Token exchange** (RFC 8693) when the agent receives a user token:
   the trait's sidecar/middleware exchanges the user token for a
   delegated token containing:
   - `sub` = original user
   - `act.sub` = `agent-runtime-<env>`
   - custom claim `agent_id` = the trait's `agentId` value
3. The delegated token is forwarded on outbound calls to
   `agentgateway`, so audit logs capture both user and agent identity.

When the IdP does not support token exchange (e.g., Okta, Auth0), the
trait falls back to **header propagation**: the user token rides
through unchanged in `Authorization`, and the agent identity rides in
a parallel header (`X-Agent-Id`). agentgateway logs both.

### BYOI considerations

The agent image contract (whether platform default or BYOI) requires:

- **Standard env vars** populated by the trait:
  - `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `OIDC_TOKEN_ENDPOINT`,
    `OIDC_ISSUER`, `OIDC_AGENT_ID`
- **Optional sidecar/library** for token exchange. Platform default
  image bundles this; BYOI authors either:
  - Use the platform's small library (recommended), or
  - Implement RFC 8693 client_credentials + token-exchange themselves.
- **Outbound HTTP middleware** that attaches the delegated token to
  agentgateway-bound requests.

The trait does not require any specific runtime — Strands, LangChain,
LlamaIndex, or custom code all work as long as the env-var contract
is honored.

## Audit identity flow

End-to-end, for a request like "Alice asks the support bot to
restart a deployment":

```
 1. Alice (browser) → UI:
    Authorization: Bearer <user_token from ui-<env> via OIDC code flow>

 2. UI → support-bot (agent service):
    Authorization: Bearer <user_token>

 3. support-bot init:
    - Token exchange: subject_token=<user_token>,
                       client_id=agent-runtime-<env>
                       requested_token_type=access_token
    - Receives <delegated_token> with:
        sub = alice
        act.sub = agent-runtime-<env>
        agent_id = support-bot

 4. support-bot → agentgateway:
    Authorization: Bearer <delegated_token>
    X-Agent-Id: support-bot   (parallel header for non-RFC-8693 IdPs)

 5. agentgateway:
    - Validates JWT against issuer/JWKS/audience
    - Authorization: jwt.groups contains "admin" (via groupsClaim)
    - Logs structured entry:
        { user: alice, agent: support-bot, action: <route>, ... }
```

The agentgateway access log is the audit record. It carries both the
human and the agent identities, satisfying the "user X did Y via
agent Z" requirement.

## Configuration per environment

Per-env values come from the agentic platform's overlays:

```yaml
# open-agentic-platform/gitops/overlays/environments/dev/agentgateway-values.yaml
env: dev
hostname: dev.peeks.dev.shapirov.people.a2z.com
groupsClaim: groups          # default; Keycloak realm uses "groups" claim
                             # mapper, customers using realm roles set
                             # this to "realm_access.roles"
tlsCertificateArn: <ARN of wildcard or per-env cert>
```

```yaml
# open-agentic-platform/gitops/overlays/environments/prod/agentgateway-values.yaml
env: prod
hostname: prod.peeks.dev.shapirov.people.a2z.com
groupsClaim: groups
tlsCertificateArn: <ARN>
```

```yaml
# open-agentic-platform/gitops/overlays/environments/control-plane/agentgateway-values.yaml
env: hub
hostname: peeks.dev.shapirov.people.a2z.com    # bare for hub
groupsClaim: groups
tlsCertificateArn: <ARN>
```

## Customer scenarios

### Scenario 1 — Customer uses the reference Keycloak

No action required. Deploy the platform with default settings; the
agentic platform integrates as documented. All clients are
provisioned automatically.

### Scenario 2 — Customer uses Okta

1. Customer disables the reference Keycloak per the platform doc
   (Path B).
2. Customer manually creates the clients in Okta with names matching
   the agentic platform's declarations:
   - `agentgateway-dev`, `agentgateway-prod`, `agentgateway-hub`
   - `agent-runtime-dev`, `agent-runtime-prod`
3. Customer populates Secrets Manager entries at the paths the
   agentic platform's declarations specify
   (`peeks/<env>/oidc/agentgateway-client`, etc.).
4. Customer sets `oidc_issuer_url` on cluster secrets to their Okta
   authorization server.
5. **Audit identity caveat**: Okta's token-exchange support is
   limited. The `oidc-identity` trait operates in
   header-propagation mode automatically.

### Scenario 3 — Customer uses Azure AD

Similar to Scenario 2. Notable differences:

- Group claim path is typically `roles` (set `groupsClaim: roles` in
  the env overlay).
- Token exchange via on-behalf-of flow works natively, so the
  `oidc-identity` trait operates in delegated-token mode.

## Implementation checklist

Phase 1 deliverables in this repo:

- [ ] `gitops/addons/oidc-clients/values.yaml` — client declarations
      consumed by the platform provisioner.
- [ ] `gitops/addons/agentgateway/` — chart parameterized per the
      platform contract; ExternalSecret pulling
      `<env>/oidc/agentgateway-client`.
- [ ] `gitops/overlays/environments/{control-plane,dev,prod}/agentgateway-values.yaml`
      with per-env `env`, `hostname`, `groupsClaim`,
      `tlsCertificateArn`.
- [ ] Migration: remove the existing in-cluster Service-ref JWKS
      configuration; replace with the public-URL pattern.
- [ ] Update fleet member values to expose `env: dev|prod|hub` so the
      agentgateway chart picks up the right Secret path and overlay.

Phase 2 deliverables:

- [ ] `oidc-identity` OAM trait definition + supporting controller or
      sidecar image.
- [ ] BYOI runtime contract documented (env vars, token-exchange
      library, outbound middleware).
- [ ] Reference agent built on Strands + MCP + AgentCore memory using
      the trait.

## Open questions

1. **OAM trait packaging** — should `oidc-identity` ship in this
   repo, or in the platform repo as a generic trait that any OAM
   workload can consume? Pro of shipping here: agent-specific
   semantics. Pro of shipping in platform: reusable for non-agent
   workloads needing delegated identity.

2. **`groupsClaim` defaults across providers** — the agentic
   platform's overlays should ship per-provider examples
   (`keycloak.yaml`, `okta.yaml`, `azuread.yaml`) showing the right
   `groupsClaim` value. Phase 1 doc deliverable.

3. **UI client lifecycle** — if a UI is deployed in a given env, who
   owns its registration? Keep it in the agentic platform's client
   declarations for now; revisit if a separate UI ships independently.

## References

- [Platform Multi-Cluster Authentication design][platform-doc]
- [RFC 8693 — OAuth 2.0 Token Exchange](https://www.rfc-editor.org/rfc/rfc8693)
- [Open Application Model (OAM)](https://oam.dev/)
- [Argo Rollouts](https://argoproj.github.io/argo-rollouts/)
