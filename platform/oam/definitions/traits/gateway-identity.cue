// gateway-identity TraitDefinition
//
// Gives a workload its own identity to AgentGateway, and makes its inbound route
// self-sufficient about credentials.
//
// 1. The agent's own identity. Mounts a projected ServiceAccount token scoped to
//    the `agentgateway` audience and points WORKLOAD_TOKEN_PATH at it, which
//    `app/identity.py:outbound()` falls back to when no caller token arrived.
//    Auto-rotated by the kubelet; no secret to manage.
//
//    This is not optional scaffolding for autonomous runs. The A2A server builds
//    the agent card at startup by invoking the agent factory once with the
//    placeholder context id `__agent_card__` (app/agent.py:25), with no request in
//    flight. `inbound_auth` is therefore unset, so the ServiceAccount token is the
//    ONLY credential available when tools are discovered. Without it the agent
//    presents nothing, the gateway returns an empty catalog to an unauthorized
//    identity, `_open()` swallows the failure into a warning, and the agent card
//    advertises zero tools — silently and permanently.
//
//    Note for spokes: the gateway only validates these tokens where its
//    workloadIdentity JWT provider is configured, which requires the
//    `eks_oidc_provider` cluster-secret annotation. That annotation is present on
//    the hub and absent on spokes, which publish the same value as
//    `eks_oidc_issuer`/`oidcProvider` instead, so the provider is currently
//    inactive on spokes. Tracked separately; it affects the startup path above.
//
// 2. Hop-1 credential handling (user -> agent). Emits a route-scoped policy that
//    restores the caller's token onto the request to the agent.
//
//    Why a policy is needed at all: jwt-policy.yaml validates the caller at the
//    edge and that validation CONSUMES the credential, so without a backend auth
//    policy the agent pod receives no `authorization` header (verified on oap-dev;
//    see credential-passthrough-policy.yaml). There is no positive way to express
//    "strip" either: `backend.auth: {}` and `backend: {}` are both rejected by the
//    CRD, so ABSENCE of a policy is the strip. That asymmetry is why this trait
//    states the agent's hop explicitly instead of relying on the Gateway-wide
//    passthrough, and it is what lets the agent -> MCP hop be governed separately
//    by the mcp-server component.
//
// Token exchange deliberately does NOT live here. It belongs on hop 2, attached to
// each MCP backend, so each server receives a token audienced to itself; see
// mcp-server.cue and ADR-6. Putting it on hop 1 would stamp `azp` with the agent
// but leave `aud` un-narrowed, which buys attribution and no containment, and it
// would require one Keycloak client per agent to do it.
//
// Rides on the pod's ServiceAccount (owned by the component, name == context.name),
// so the token's `sub` (system:serviceaccount:<ns>:<name>) is the workload identity
// and one name anchors ServiceAccount, container, component and HTTPRoute (ADR-3).
"gateway-identity": {
	alias:       ""
	annotations: {}
	attributes: {
		appliesToWorkloads: ["deployments.apps", "rollouts.argoproj.io"]
		conflictsWith: []
		podDisruptive:   true
		workloadRefPath: ""
	}
	description: "Give a workload its identity to AgentGateway: a projected ServiceAccount token, plus route-scoped handling of the caller's credential"
	labels: {}
	type: "trait"
}

template: {
	parameter: {
		// +usage=Audience stamped into the projected ServiceAccount token; must match the gateway's expected audience for workload identity.
		audience: *"agentgateway" | string
		// +usage=Container to mount the token into (defaults to the component name)
		containerName: *context.name | string
	}

	_mountDir:   "/var/run/secrets/agentgateway"
	_tokenMount: {
		name:      "agentgateway-token"
		mountPath: _mountDir
		readOnly:  true
	}

	outputs: {
		// Targets the HTTPRoute the agent component emits as context.name when
		// registerWithGateway is true (agent.cue:235-243). When that is false no route
		// exists and this policy never attaches, which is harmless: an agent not
		// registered with the gateway receives no gateway traffic.
		//
		// Route-scoped, so it overrides the Gateway-wide credential-passthrough-policy
		// for this route only (precedence: Gateway < Listener < Route < Route Rule <
		// Backend). Same effect as that policy today; stating it here means the agent's
		// hop stays correct if the Gateway-wide one is ever narrowed or removed.
		"\(context.name)-caller-credential": {
			apiVersion: "agentgateway.dev/v1alpha1"
			kind:       "AgentgatewayPolicy"
			metadata: {
				name:      context.name + "-caller-credential"
				namespace: context.namespace
				labels: "app.kubernetes.io/name": context.name
			}
			spec: {
				targetRefs: [{
					group: "gateway.networking.k8s.io"
					kind:  "HTTPRoute"
					name:  context.name
				}]
				backend: auth: passthrough: {}
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
