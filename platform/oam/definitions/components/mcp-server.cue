// mcp-server ComponentDefinition
//
// An MCP server managed by an Argo Rollout (blue-green) that also registers
// itself with AgentGateway. Aligned with `service-rollout`: it owns a dedicated
// ServiceAccount (name == context.name) and names its container context.name,
// so it is the workload's single identity anchor — attach `aws-service-identity`
// and/or `gateway-identity` traits to grant AWS / AgentGateway identities with
// no extra wiring. Workload parameters mirror `service-rollout`; the MCP-specific
// additions are the AgentgatewayBackend, the /mcp/<name> HTTPRoute, and an
// optional tool-level authorization policy.
//
// NOTE: the workload/SA/Service skeleton is intentionally duplicated with
// `service-rollout` (see decision log): `vela def render` renders each
// definition from a self-contained file and does not resolve local CUE imports,
// so shared-template reuse would require a cluster-registered cue.oam.dev
// Package. The duplication is the accepted, bounded cost of keeping mcp-server a
// first-class, self-contained component.
"mcp-server": {
	alias:       ""
	annotations: {}
	attributes: {
		workload: definition: {
			apiVersion: "argoproj.io/v1alpha1"
			kind:       "Rollout"
		}
		status: healthPolicy: #"isHealth: (context.output.status.phase != _|_) && (context.output.status.phase == "Healthy")"#
	}
	description: "MCP server (Argo Rollout blue-green) with a dedicated ServiceAccount and AgentGateway registration"
	labels: {}
	type: "component"
}

template: {
	output: {
		apiVersion: "argoproj.io/v1alpha1"
		kind:       "Rollout"
		metadata: {
			name:      context.name
			namespace: context.namespace
			labels: {
				"app.kubernetes.io/name":      context.name
				"app.kubernetes.io/component": "mcp-server"
			}
			if parameter.description != _|_ {
				annotations: "mcp.dev/description": parameter.description
			}
		}
		spec: {
			// Only set replicas when the developer actually asked for a count. Rendering
			// it unconditionally makes KubeVela fight any autoscaler: an HPA writes
			// spec.replicas through the Rollout's /scale subresource, KubeVela reconciles
			// it back to the declared value, and the pair flaps (observed going 2 -> 1,
			// killing a pod, -> 2). Omit the parameter to hand ownership of replicas to
			// an hpa/cpuscaler trait.
			if parameter.replicas != _|_ {
				replicas: parameter.replicas
			}
			strategy: blueGreen: {
				activeService:        context.name + "-stable"
				previewService:       context.name + "-preview"
				autoPromotionEnabled: parameter.autoPromotionEnabled
				if parameter.autoPromotionSeconds != _|_ {
					autoPromotionSeconds: parameter.autoPromotionSeconds
				}
				if parameter.scaleDownDelaySeconds != _|_ {
					scaleDownDelaySeconds: parameter.scaleDownDelaySeconds
				}
			}
			selector: matchLabels: "app.kubernetes.io/name": context.name
			template: {
				metadata: labels: "app.kubernetes.io/name": context.name
				spec: {
					serviceAccountName: context.name
					containers: [{
						name:  context.name
						image: parameter.image
						if parameter.command != _|_ {
							command: parameter.command
						}
						if parameter.args != _|_ {
							args: parameter.args
						}
						ports: [{
							name:          "mcp"
							containerPort: parameter.port
							protocol:      "TCP"
						}]
						if len(parameter.env) > 0 {
							env: parameter.env
						}
						livenessProbe: {
							if parameter.healthPath != _|_ {
								httpGet: {
									path: parameter.healthPath
									port: parameter.port
								}
							}
							if parameter.healthPath == _|_ {
								tcpSocket: port: parameter.port
							}
							initialDelaySeconds: 10
							periodSeconds:       30
						}
						readinessProbe: {
							if parameter.readinessPath != _|_ {
								httpGet: {
									path: parameter.readinessPath
									port: parameter.port
								}
							}
							if parameter.readinessPath == _|_ && parameter.healthPath != _|_ {
								httpGet: {
									path: parameter.healthPath
									port: parameter.port
								}
							}
							if parameter.readinessPath == _|_ && parameter.healthPath == _|_ {
								tcpSocket: port: parameter.port
							}
							initialDelaySeconds: 5
							periodSeconds:       10
						}
						if parameter.resources != _|_ {
							resources: parameter.resources
						}
					}]
				}
			}
		}
	}

	outputs: {
		// Dedicated ServiceAccount — the workload's identity anchor (name ==
		// context.name), so aws-service-identity / gateway-identity attach cleanly.
		serviceAccount: {
			apiVersion: "v1"
			kind:       "ServiceAccount"
			metadata: {
				name:      context.name
				namespace: context.namespace
				labels: "app.kubernetes.io/name": context.name
			}
		}

		// Stable service (active) — targeted by the AgentgatewayBackend.
		//
		// The agentgateway.dev/target label is what the backend's selector matches. It
		// exists because both the stable and preview Services carry
		// app.kubernetes.io/name, so selecting on that alone would send live sessions to
		// preview pods mid-rollout.
		stableService: {
			apiVersion: "v1"
			kind:       "Service"
			metadata: {
				name:      context.name + "-stable"
				namespace: context.namespace
				labels: {
					"app.kubernetes.io/name": context.name
					"agentgateway.dev/target": context.name
				}
			}
			spec: {
				selector: "app.kubernetes.io/name": context.name
				ports: [{
					name:        "mcp"
					port:        parameter.servicePort
					targetPort:  parameter.port
					protocol:    "TCP"
					appProtocol: "agentgateway.dev/mcp"
				}]
				type: "ClusterIP"
			}
		}

		// Preview service (for blue-green)
		previewService: {
			apiVersion: "v1"
			kind:       "Service"
			metadata: {
				name:      context.name + "-preview"
				namespace: context.namespace
				labels: "app.kubernetes.io/name": context.name
			}
			spec: {
				selector: "app.kubernetes.io/name": context.name
				ports: [{
					name:        "mcp"
					port:        parameter.servicePort
					targetPort:  parameter.port
					protocol:    "TCP"
					appProtocol: "agentgateway.dev/mcp"
				}]
				type: "ClusterIP"
			}
		}

		// AgentgatewayBackend — static target pointing at the stable service.
		mcpBackend: {
			apiVersion: "agentgateway.dev/v1alpha1"
			kind:       "AgentgatewayBackend"
			metadata: {
				name:      context.name + "-backend"
				namespace: context.namespace
				labels: "app.kubernetes.io/name": context.name
			}
			// A selector target, not a static host. This is what makes session affinity
			// possible: with a static host the gateway talks to the Service's ClusterIP and
			// kube-proxy picks a pod per connection, so the gateway has no pod to pin a
			// session to. Upstream documents this explicitly: stateful session routing and
			// session affinity require non-static, selector-based targets. Measured with a
			// static target and 2 replicas: connect succeeded and the next call failed with
			// "no valid session ID provided".
			//
			// The protocol comes from the Service's appProtocol (agentgateway.dev/mcp) in
			// this form, so parameter.mcpProtocol only applies to the static fallback.
			spec: {
				mcp: {
					targets: [{
						name: context.name + "-target"
						selector: services: matchLabels: "agentgateway.dev/target": context.name
					}]
					if parameter.sessionAffinity {
						sessionRouting: "Stateful"
					}
					if !parameter.sessionAffinity {
						sessionRouting: "Stateless"
					}
				}
			}
		}

		// HTTPRoute — registers the MCP server with the gateway at /mcp/<name>.
		if parameter.registerWithGateway {
			gatewayRoute: {
				apiVersion: "gateway.networking.k8s.io/v1"
				kind:       "HTTPRoute"
				metadata: {
					name:      context.name
					namespace: context.namespace
					labels: "app.kubernetes.io/name": context.name
				}
				spec: {
					parentRefs: [{
						name:      "agentgateway-proxy"
						namespace: parameter.gatewayNamespace
					}]
					rules: [{
						matches: [{
							path: {
								type:  "PathPrefix"
								value: "/mcp/" + context.name
							}
						}]
						backendRefs: [{
							group: "agentgateway.dev"
							kind:  "AgentgatewayBackend"
							name:  context.name + "-backend"
						}]
					}]
				}
			}
		}

		// Optional: AgentgatewayPolicy for tool-level authorization (CEL).
		if parameter.authPolicy != _|_ && len(parameter.authPolicy.matchExpressions) > 0 {
			toolAccessPolicy: {
				apiVersion: "agentgateway.dev/v1alpha1"
				kind:       "AgentgatewayPolicy"
				metadata: {
					name:      context.name + "-tool-access"
					namespace: context.namespace
					labels: "app.kubernetes.io/name": context.name
				}
				spec: {
					targetRefs: [{
						group: "agentgateway.dev"
						kind:  "AgentgatewayBackend"
						name:  context.name + "-backend"
					}]
					backend: mcp: authorization: {
						action: parameter.authPolicy.action
						policy: matchExpressions: parameter.authPolicy.matchExpressions
					}
				}
			}
		}

		// Hop 2 (agent -> this server): exchange the caller's token for one whose `aud` is
		// THIS server, so the server receives a credential it can validate as its own.
		// This is what makes the MCP spec's audience requirement satisfiable; forwarding
		// the caller's realm-wide token is the anti-pattern the spec names.
		//
		// Safe to emit as a SEPARATE policy from the tool-access one above, despite both
		// targeting this backend: they set DIFFERENT fields (`backend.auth` vs
		// `backend.mcp`), and the merge is field-level, so they compose instead of tying.
		//
		// ONE audience, which is also Keycloak's own recommendation. Keycloak's `audience`
		// parameter FILTERS the set its client scopes already produce and rejects the whole
		// request if any requested audience does not resolve, so a single audience per
		// policy keeps one misconfigured server from breaking the others.
		//
		// Prerequisites, both verified against live Keycloak:
		//   - the exchange client needs standard token exchange enabled;
		//   - the exchange client must appear in the SUBJECT token's `aud`, otherwise
		//     Keycloak returns access_denied "Client is not within the token audience" —
		//     this holds even when no audience parameter is sent. The platform arranges it
		//     with an audience protocol mapper on each caller client (crossplane-keycloak
		//     chart, exchange.callerClients), so no per-user step is needed.
		if parameter.tokenExchange {
			credentialPolicy: {
				apiVersion: "agentgateway.dev/v1alpha1"
				kind:       "AgentgatewayPolicy"
				metadata: {
					name:      context.name + "-credential"
					namespace: context.namespace
					labels: "app.kubernetes.io/name": context.name
				}
				spec: {
					targetRefs: [{
						group: "agentgateway.dev"
						kind:  "AgentgatewayBackend"
						name:  context.name + "-backend"
					}]
					backend: auth: oauthTokenExchange: {
						grantType: "TokenExchange"
						// group and kind are REQUIRED: backendRef.kind defaults to `Service`.
						// keycloak-jwks is a generic static route to Keycloak's public host on
						// 443 with the path supplied by the consumer, so reusing it for the token
						// endpoint is correct despite the jwks-shaped name. Public rather than a
						// cluster-internal Service, because Keycloak is hub-only and a Service
						// backendRef resolves nothing on a spoke (see keycloak-backend.yaml).
						backendRef: {
							group:     "agentgateway.dev"
							kind:      "AgentgatewayBackend"
							name:      "keycloak-jwks"
							namespace: parameter.gatewayNamespace
						}
						path: parameter.tokenPath
						audiences: [parameter.audience]
						clientAuth: {
							clientId: parameter.exchangeClientId
							// Reads key `clientSecret`, which is exactly what the Crossplane
							// provider's writeConnectionSecretToRef emits, so no key override.
							// secretRef has NO namespace field, so this Secret must exist in THIS
							// namespace; the platform replicates it wherever MCP servers run.
							secretRef: name: parameter.exchangeSecretName
						}
					}
				}
			}

			// The Keycloak client that IS this server's audience. Keycloak's `audience`
			// parameter FILTERS an already-resolvable set, so without a client of this id the
			// exchange fails with `invalid_request - Requested audience not available`.
			// Confidential and flow-less: nothing ever authenticates AS this client, it exists
			// to be named as an audience.
			identityClient: {
				apiVersion: "openidclient.keycloak.crossplane.io/v1alpha2"
				kind:       "Client"
				metadata: {
					name: parameter.audience
					labels: "app.kubernetes.io/name": context.name
				}
				spec: {
					providerConfigRef: name: "default"
					forProvider: {
						realmId:             parameter.keycloakRealm
						clientId:            parameter.audience
						name:                parameter.audience
						description:         "Audience for MCP server " + context.name + " (managed by OAP)"
						accessType:          "CONFIDENTIAL"
						standardFlowEnabled: false
						implicitFlowEnabled: false
						directAccessGrantsEnabled: false
						serviceAccountsEnabled:    false
					}
				}
			}

			// Makes this server's audience resolvable FROM the exchange client. Attached to the
			// exchange client, not to this one: the filter runs against the audiences the
			// exchanging client can already produce. clientIdRef resolves that client's Keycloak
			// UUID from its Crossplane resource, so no UUID is ever written down.
			//
			// Every MCP server adds one mapper here, so an UNAUDIENCED exchange from this client
			// would carry all of them at once. That is exactly why each policy sends its own
			// single `audience`: the filter is what delivers one-server-per-token.
			identityAudienceMapper: {
				apiVersion: "openidgroup.keycloak.crossplane.io/v1alpha1"
				kind:       "AudienceProtocolMapper"
				metadata: {
					name: parameter.audience + "-aud"
					labels: "app.kubernetes.io/name": context.name
				}
				spec: {
					providerConfigRef: name: "default"
					forProvider: {
						realmId: parameter.keycloakRealm
						name:    parameter.audience + "-aud"
						clientIdRef: name: parameter.exchangeClientId
						includedClientAudience: parameter.audience
						addToAccessToken:       true
						addToIdToken:           false
					}
				}
			}

			// Copies the exchange client's Secret into THIS namespace, because
			// clientAuth.secretRef has no namespace field and the policy can only read a Secret
			// beside itself. references[].patchesFrom reads the source Secret's data key and
			// patches it in, so the value is never templated into git.
			exchangeSecretCopy: {
				apiVersion: "kubernetes.crossplane.io/v1alpha2"
				kind:       "Object"
				metadata: {
					name: context.name + "-exchange-secret"
					labels: "app.kubernetes.io/name": context.name
				}
				spec: {
					providerConfigRef: name: "default"
					references: [{
						patchesFrom: {
							apiVersion: "v1"
							kind:       "Secret"
							name:       parameter.exchangeSecretName
							namespace:  parameter.exchangeSecretNamespace
							fieldPath:  "data.clientSecret"
						}
						toFieldPath: "data.clientSecret"
					}]
					forProvider: manifest: {
						apiVersion: "v1"
						kind:       "Secret"
						type:       "Opaque"
						metadata: {
							name:      parameter.exchangeSecretName
							namespace: context.namespace
						}
					}
				}
			}
		}
	}

	parameter: {
		// +usage=Container image
		image: string
		// +usage=Human-readable description (annotation only)
		description?: string
		// +usage=Number of replicas. OMIT this to let an autoscaler (hpa/cpuscaler
		// trait) own replicas: when set, KubeVela keeps reconciling it and would fight
		// the HPA. Omitted leaves the field off the Rollout, which Argo treats as 1.
		// Only safe to autoscale if the server holds no pod-local session state, or if
		// the gateway provides session affinity.
		replicas?: int
		// +usage=Pin each MCP session to one backend pod (sessionRouting: Stateful).
		// ON by default, because an MCP session is stateful by specification and most
		// servers keep per-session state in memory. Turn it off ONLY for a server that
		// is genuinely stateless, where every request carries its full context; that
		// lets requests spread across all pods instead of following a session.
		sessionAffinity: *true | bool
		// +usage=Container port the MCP server listens on (FastMCP default 8000)
		port: *8000 | int
		// +usage=Service port exposed by the stable/preview Services
		servicePort: *80 | int
		// +usage=Optional container command override
		command?: [...string]
		// +usage=Optional container args
		args?: [...string]
		// +usage=Environment variables
		env: *[] | [...{
			name:  string
			value: string
		}]
		// +usage=HTTP path for the liveness probe; if unset, a TCP socket probe is used.
		// Liveness should report only that the process is serving, never that a
		// dependency is reachable, or a slow/failing dependency causes restart loops.
		healthPath?: string
		// +usage=HTTP path for the readiness probe; defaults to healthPath. Set this
		// separately when the server needs slow startup work (e.g. an AWS call that
		// can fail for minutes while IAM propagates) before it can serve: liveness on
		// a path that is up immediately, readiness on one that gates traffic.
		readinessPath?: string
		// +usage=Blue-green auto-promotion
		autoPromotionEnabled:   *true | bool
		autoPromotionSeconds?:  int
		scaleDownDelaySeconds?: int
		// +usage=MCP transport protocol advertised to AgentGateway
		mcpProtocol: *"StreamableHTTP" | "SSE"
		// +usage=Register an HTTPRoute on the gateway at /mcp/<name>
		registerWithGateway: *true | bool
		// +usage=Namespace of the agentgateway-proxy Gateway
		gatewayNamespace: *"agentgateway-system" | string
		// +usage=Resource requests/limits
		resources?: {
			requests?: {
				cpu?:    string
				memory?: string
			}
			limits?: {
				cpu?:    string
				memory?: string
			}
		}
		// +usage=Exchange the caller's token for one audienced to THIS server before forwarding (RFC 8693, at the gateway). This is what lets the server validate the token as its own, which the MCP spec requires of it. Needs a Keycloak client whose id matches `audience`, and the platform's exchange client to be enabled. NOTE: incompatible with callers that present an agent ServiceAccount token — verified live. The exchange applies to every caller of this backend and treats whatever bearer arrived as a Keycloak subject_token; an EKS ServiceAccount token is rejected by Keycloak with "invalid_request - Invalid token", surfacing at the gateway as a 500 on mcp.method.name=initialize. Keycloak token exchange is internal-to-itself only. Use the platform mcpAccess grant (agent-gateway chart) for agent-identity callers and tokenExchange for Keycloak-identity callers, not both on one server.
		tokenExchange: *false | bool
		// +usage=Keycloak client id representing this MCP server; becomes the exchanged token's `aud`. Defaults to the component name so the Keycloak client, component, Service, backend and route all share one name. Only used when tokenExchange is true.
		audience: *context.name | string
		// +usage=Client id the gateway authenticates as when performing the exchange. Platform-supplied; do not set in a developer's Application.
		exchangeClientId: *"{{ .Values.global.keycloak.exchangeClientId }}" | string
		// +usage=Secret holding that client's credential under key `clientSecret`. Must exist in this namespace, because the policy's secretRef has no namespace field. Platform-supplied.
		exchangeSecretName: *"{{ .Values.global.keycloak.exchangeSecretName }}" | string
		// +usage=Keycloak realm the exchange objects live in. Platform-supplied.
		keycloakRealm: *"{{ .Values.global.keycloak.realm }}" | string
		// +usage=Namespace holding the exchange client's generated Secret, replicated from here into this component's namespace. Platform-supplied.
		exchangeSecretNamespace: *"{{ .Values.global.keycloak.exchangeSecretNamespace }}" | string
		// +usage=Keycloak token endpoint path. Defaults to the platform's realm so the same OAM Application stays portable across clusters; override only for a non-default IdP layout.
		tokenPath: *"{{ .Values.global.keycloak.pathPrefix }}/realms/{{ .Values.global.keycloak.realm }}/protocol/openid-connect/token" | string
		// +usage=Tool-level authorization policy (CEL-based)
		authPolicy?: {
			// The CRD enum is Allow | Deny | Require. Prefer Allow or Require: the CRD
			// warns "Deny is not recommended because expression failures fail to deny",
			// i.e. a CEL evaluation error in a Deny rule fails OPEN.
			action:           *"Allow" | "Deny" | "Require"
			matchExpressions: [...string]
		}
	}
}
