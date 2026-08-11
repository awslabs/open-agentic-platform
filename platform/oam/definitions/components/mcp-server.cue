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
		stableService: {
			apiVersion: "v1"
			kind:       "Service"
			metadata: {
				name:      context.name + "-stable"
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
			spec: mcp: targets: [{
				name: context.name + "-target"
				static: {
					host:     context.name + "-stable." + context.namespace + ".svc.cluster.local"
					port:     parameter.servicePort
					protocol: parameter.mcpProtocol
				}
			}]
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
		// +usage=Tool-level authorization policy (CEL-based)
		authPolicy?: {
			action:           *"Allow" | "Deny"
			matchExpressions: [...string]
		}
	}
}
