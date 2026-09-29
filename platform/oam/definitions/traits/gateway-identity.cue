// gateway-identity TraitDefinition
//
// Gives a workload its identity to AgentGateway/MCP. Two halves of one question
// ("what credential does this agent present?"), which `app/identity.py:outbound()`
// already resolves in one place: forward the caller's bearer if one arrived,
// otherwise fall back to the agent's own projected ServiceAccount token.
//
// 1. ALWAYS — the agent's own identity. Mounts a projected ServiceAccount token
//    scoped to the `agentgateway` audience and points WORKLOAD_TOKEN_PATH at it.
//    AgentGateway validates it against the cluster's EKS OIDC issuer (see the
//    agent-gateway workloadIdentity provider). Auto-rotated by the kubelet; no
//    secret to manage.
//
//    This is not optional scaffolding for autonomous runs. The A2A server builds
//    the agent card at startup by invoking the agent factory once with the
//    placeholder context id `__agent_card__` (app/agent.py:25), with no request in
//    flight. `inbound_auth` is therefore unset, so the ServiceAccount token is the
//    ONLY credential available when tools are discovered. Without it the agent
//    presents nothing, agentgateway returns an empty catalog to an unauthorized
//    identity, `_open()` swallows the failure into a warning, and the agent card
//    advertises zero tools — silently and permanently.
//
// 2. OPT-IN via `delegateTo` — delegated caller identity. Attaches an RFC 8693
//    token exchange to the agent's own HTTPRoute, so the caller's token is
//    narrowed BEFORE it reaches the agent: `aud` bounded to the named MCP servers
//    and `azp` stamped with this agent. The agent forwards the result unchanged.
//
//    Why hop 1 and not inside the agent: `clientAuth.clientId` is what Keycloak
//    stamps into `azp`, so the exchanging client must be per-agent; and the
//    gateway validates exactly one JWT per request, so user and agent identity
//    must coexist in one token as `sub` and `azp`. Keycloak accepts `actor_token`
//    and silently ignores it (HTTP 200, no `act`), so `azp` is the only available
//    carrier. See docs/architecture/agent-identity-and-token-exchange.md (ADR-6).
//
//    `delegateTo` is both the scope and the enablement gate. Empty (the default)
//    emits nothing new, so existing Applications are unaffected and the platform
//    keeps today's Gateway-scoped credential passthrough. Policy precedence is
//    Gateway < Listener < Route < Route Rule < Backend, so this route-scoped
//    policy overrides that passthrough for this agent only. The two compose;
//    nothing needs removing first.
//
//    Ordering: the exchange needs a Keycloak client and its secret to exist. The
//    trait emits an IdpClient claim and gates the pod on it with an init container,
//    the same arrangement aws-service-identity uses for PodIdentity. Without that
//    gate the pod would start, fail every exchange, and look healthy.
//
// Rides on the pod's ServiceAccount (owned by the component, name == context.name),
// so the token's `sub` (system:serviceaccount:<ns>:<name>) is the workload identity,
// and `clientId == context.name` keeps one identity anchor across ServiceAccount,
// container, component, HTTPRoute and IdP client (ADR-3).
"gateway-identity": {
	alias:       ""
	annotations: {}
	attributes: {
		appliesToWorkloads: ["deployments.apps", "rollouts.argoproj.io"]
		conflictsWith: []
		podDisruptive:   true
		workloadRefPath: ""
	}
	description: "Give a workload its identity to AgentGateway: projected ServiceAccount token, plus optional token exchange that narrows the caller's credential to named MCP servers"
	labels: {}
	type: "trait"
}

template: {
	parameter: {
		// +usage=Audience stamped into the projected ServiceAccount token; must match the gateway's expected audience. This is the AGENT's own identity, unrelated to delegateTo.
		audience: *"agentgateway" | string
		// +usage=Container to mount the token into (defaults to the component name)
		containerName: *context.name | string
		// +usage=MCP server names this agent may act on behalf of a caller for. Non-empty enables token exchange at the gateway, narrowing the caller's token to exactly these audiences. Empty (default) leaves the platform's credential passthrough in place.
		delegateTo: *[] | [...string]
		// +usage=Keycloak token endpoint path. Defaults to the platform's realm so the same OAM Application is portable across clusters; override only for a non-default IdP layout.
		tokenPath: *"{{ .Values.global.keycloak.pathPrefix }}/realms/{{ .Values.global.keycloak.realm }}/protocol/openid-connect/token" | string
		// +usage=Keycloak realm the agent's IdP client is created in. Supplied by the platform; do not set in a developer's Application.
		realm: *"{{ .Values.global.keycloak.realm }}" | string
		// +usage=Namespace holding the shared Keycloak backend and the gateway. Supplied by the platform.
		gatewayNamespace: *"agentgateway-system" | string
		// +usage=Distroless kubectl image for the IdpClient-readiness init gate (entrypoint = kubectl). Chainguard kubectl:latest, pinned by multi-arch index digest (amd64+arm64) for immutability.
		waitImage: *"public.ecr.aws/chainguard/kubectl:latest@sha256:5cd49041fed950723afaefcd141a163e5a5306f243841510d3e1e3667b0cdfb9" | string
	}

	_mountDir:   "/var/run/secrets/agentgateway"
	_tokenMount: {
		name:      "agentgateway-token"
		mountPath: _mountDir
		readOnly:  true
	}

	// Secret the Composition writes the generated client credential into, and that
	// the exchange policy reads `clientSecret` from. Deterministic so both sides
	// agree without a lookup.
	_idpSecretName: context.name + "-idp"

	outputs: {
		if len(parameter.delegateTo) > 0 {
			// Abstract claim for the agent's Keycloak client, satisfied by a
			// swappable Composition (same arrangement as aws-service-identity's
			// PodIdentity claim). The Composition must set the client's
			// `standard.token.exchange.enabled` attribute — verified mandatory:
			// without it Keycloak rejects the exchange with 400 invalid_request —
			// and must report Ready, because the init container below waits on it.
			"\(context.name)-idp-client": {
				apiVersion: "platform.gitops.io/v1alpha1"
				kind:       "IdpClient"
				metadata: {
					name:      context.name
					namespace: context.namespace
					labels: "app.kubernetes.io/name": context.name
				}
				spec: {
					clientId: context.name
					realm:    parameter.realm
					// Target clients this client may exchange its callers' tokens
					// toward. `audiences` FILTERS, it cannot add: each target needs a
					// client role mapped onto the user-facing client or Keycloak
					// returns "Requested audience not available".
					audiences: parameter.delegateTo
					writeConnectionSecretToRef: name: _idpSecretName
				}
			}

			// RBAC so the pod's ServiceAccount can read its own IdpClient, used by
			// the readiness init container. Namespaced and read-only.
			"\(context.name)-idpclient-reader-role": {
				apiVersion: "rbac.authorization.k8s.io/v1"
				kind:       "Role"
				metadata: {
					name:      context.name + "-idpclient-reader"
					namespace: context.namespace
				}
				rules: [{
					apiGroups: ["platform.gitops.io"]
					resources: ["idpclients"]
					verbs: ["get", "list", "watch"]
				}]
			}
			"\(context.name)-idpclient-reader-binding": {
				apiVersion: "rbac.authorization.k8s.io/v1"
				kind:       "RoleBinding"
				metadata: {
					name:      context.name + "-idpclient-reader"
					namespace: context.namespace
				}
				roleRef: {
					apiGroup: "rbac.authorization.k8s.io"
					kind:     "Role"
					name:     context.name + "-idpclient-reader"
				}
				subjects: [{
					kind:      "ServiceAccount"
					name:      context.name
					namespace: context.namespace
				}]
			}

			// Route-scoped exchange. Targets the HTTPRoute the agent component emits
			// as context.name when registerWithGateway is true (agent.cue:235-243),
			// so it overrides the Gateway-scoped passthrough for this agent only.
			//
			// subjectToken is omitted deliberately: it defaults to Authorization
			// Bearer / AccessToken, which is the caller's token. location is omitted
			// for the same reason — it defaults to `Authorization: Bearer`, so the
			// exchanged token replaces the original on the way to the agent.
			"\(context.name)-delegation-policy": {
				apiVersion: "agentgateway.dev/v1alpha1"
				kind:       "AgentgatewayPolicy"
				metadata: {
					name:      context.name + "-delegation"
					namespace: context.namespace
					labels: "app.kubernetes.io/name": context.name
				}
				spec: {
					targetRefs: [{
						group: "gateway.networking.k8s.io"
						kind:  "HTTPRoute"
						name:  context.name
					}]
					backend: auth: oauthTokenExchange: {
						grantType: "TokenExchange"
						// group and kind are REQUIRED here: backendRef.kind defaults to
						// `Service`, and keycloak-jwks is an AgentgatewayBackend. It is a
						// generic static route to Keycloak's public host on 443 with the
						// path supplied by the consumer, so reusing it for the token
						// endpoint is correct despite the jwks-shaped name. Deliberately
						// public rather than a cluster-internal Service because Keycloak
						// is hub-only and a Service backendRef resolves nothing on a
						// spoke (see keycloak-backend.yaml).
						backendRef: {
							group:     "agentgateway.dev"
							kind:      "AgentgatewayBackend"
							name:      "keycloak-jwks"
							namespace: parameter.gatewayNamespace
						}
						path:      parameter.tokenPath
						audiences: parameter.delegateTo
						clientAuth: {
							// clientId becomes `azp` in the exchanged token: this is how a
							// downstream backend learns which agent acted.
							clientId: context.name
							secretRef: name: _idpSecretName
						}
					}
				}
			}
		}
	}

	patch: spec: template: spec: {
		// +patchKey=name
		volumes: [{
			name: "agentgateway-token"
			projected: sources: [{
				serviceAccountToken: {
					audience:          parameter.audience
					expirationSeconds: 3600
					path:              "token"
				}
			}]
		}]

		if len(parameter.delegateTo) > 0 {
			// +patchKey=name
			// Control-plane readiness gate: distroless kubectl (entrypoint = kubectl)
			// blocks until the IdpClient reports Ready, i.e. the Composition has
			// created the Keycloak client and written its secret. Uses the pod's
			// ServiceAccount (in-cluster config) plus the Role/RoleBinding above. If
			// the claim does not exist yet the init container fails and the kubelet
			// retries it, which is the intended backoff.
			initContainers: [{
				name:  "wait-for-idp-client"
				image: parameter.waitImage
				args: [
					"wait", "--for=condition=Ready",
					"idpclients.platform.gitops.io/\(context.name)",
					"-n", context.namespace,
					"--timeout=300s",
				]
			}]
		}

		// +patchKey=name
		containers: [{
			name: parameter.containerName
			// +patchKey=name
			env: [{
				name:  "WORKLOAD_TOKEN_PATH"
				value: "\(_mountDir)/token"
			}]
			// +patchKey=name
			volumeMounts: [_tokenMount]
		}]
	}
}
