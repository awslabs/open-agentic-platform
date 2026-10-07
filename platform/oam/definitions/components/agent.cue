// Agent ComponentDefinition with blue-green deployment and pluggable memory
import (
	"strings"
	"encoding/json"
	"list"
)

agent: {
	alias: ""
	annotations: {}
	attributes: workload: definition: {
		apiVersion: "argoproj.io/v1alpha1"
		kind:       "Rollout"
	}
	description: "Declarative agent with blue-green deployment and pluggable memory"
	labels: {}
	type: "component"
}

template: {
	// PLATFORM-OWNED isolation runtime. NOT a parameter: the leading underscore
	// makes this a CUE local, so it is structurally unreachable from a developer's
	// `properties` block. That is what enforces "the platform picks the VMM"
	// rather than a convention a caller could ignore.
	//
	// The value is a Helm placeholder substituted when Argo renders the
	// oam-agent-components chart, BEFORE KubeVela ever parses this CUE -- the same
	// mechanism .kiro/steering/oam-authoring.md §1 prescribes for region/account,
	// and the one agentcore-memory.cue and aws-service-identity.cue already use.
	// The chart's values.yaml carries the fallback (kata-clh); a per-cluster
	// override sets global.sandboxRuntimeClass to kata-qemu / kata-fc.
	//
	// Deliberate deviation from steering §1: that rule prescribes an OVERRIDABLE
	// default -- a starred placeholder disjoined with `string`. A local is
	// non-overridable on purpose, because the isolation class is a security
	// boundary, not a portability knob.
	//
	// DO NOT write a literal Helm action (a double-brace pair) inside a comment in
	// this file. `vela def render` copies these comments verbatim into the generated
	// ComponentDefinition, and Helm parses that whole file as a Go template before
	// KubeVela ever sees it -- so a brace pair whose contents are not a valid Go
	// template expression fails the render of the ENTIRE oam-agent-components chart
	// with `unexpected <.> in operand`, taking every component and trait with it,
	// not just this one. Describe the pattern in prose instead, as above.
	_sandboxRuntimeClass: "{{ .Values.global.sandboxRuntimeClass }}"

	// FAIL CLOSED. If the platform never configured an isolation runtime and a
	// developer asks for one, emitting a pod with no runtimeClassName would
	// schedule an ordinary runc container: isolation requested, isolation not
	// delivered, and nothing anywhere reports a problem. Fail the render instead.
	//
	// This covers only the UNCONFIGURED-PLATFORM case. A class that is configured
	// but has no matching node cannot be caught here -- the OAM layer cannot read
	// node labels at render time -- and surfaces at runtime as Pending pods, which
	// leave the Rollout un-progressed and therefore not-Ready rather than
	// fake-green.
	if parameter.sandbox && _sandboxRuntimeClass == "" {
		_|_ // "sandbox: true requires global.sandboxRuntimeClass on the
		//  oam-agent-components chart; this cluster has no isolation runtime
		//  configured."
	}

	// Build memory env vars from config
	let _memoryEnv = [
		if parameter.memory != _|_ {
			{
				name:  "MEMORY_PROVIDER"
				value: parameter.memory.provider
			}
		},
		if parameter.memory != _|_ && parameter.memory.config != _|_ {
			{
				name:  "MEMORY_CONFIG"
				value: json.Marshal(parameter.memory.config)
			}
		},
	]

	output: {
		apiVersion: "argoproj.io/v1alpha1"
		kind:       "Rollout"
		metadata: {
			name:      context.name
			namespace: context.namespace
			labels: {
				"app.kubernetes.io/name":      context.name
				"app.kubernetes.io/component": "ai-agent"
			}
		}
		spec: {
			// Only set replicas when asked, so an hpa/cpuscaler trait can own the count
			// without KubeVela reconciling it back and flapping.
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
			selector: matchLabels: {
				"app.kubernetes.io/name": context.name
			}
			template: {
				metadata: labels: {
					"app.kubernetes.io/name": context.name
				}
				spec: {
					serviceAccountName: context.name

					// Isolation: this single field is the whole node-placement
					// mechanism. Kubernetes' built-in RuntimeClass admission
					// controller force-merges the class's scheduling.nodeSelector
					// and scheduling.tolerations onto the pod, and applies its
					// overhead.podFixed for scheduling and kubelet accounting --
					// so nothing here needs to know about the Kata node pool, its
					// taint, or the VMM's memory cost.
					if parameter.sandbox {
						runtimeClassName: _sandboxRuntimeClass
					}

					containers: [{
						name:  context.name
						image: parameter.image
						// Auto-instrument for tracing. This lived only in the generated YAML,
						// so every regeneration silently deleted it and broke tracing
						// (issue #50). It belongs in the image via AGENT_OBSERVABILITY_ENABLED
						// rather than a hardcoded Python command, but until then it lives here,
						// where regeneration preserves it.
						if parameter.observability.mode == "decentralized" {
							command: ["opentelemetry-instrument", "python", "-m", "app.main"]
						}
						ports: [{
							name:          "a2a"
							containerPort: 8083
							protocol:      "TCP"
						}]
						env: list.Concat([[
							{name: "AGENT_NAME", value: context.name},
							{name: "AGENT_DESCRIPTION", value: parameter.description},
							{name: "MODEL_ID", value: parameter.modelConfig.modelId},
							{name: "SYSTEM_PROMPT", value: parameter.systemMessage},
							{name: "PORT", value: "8083"},
							{name: "LLM_GATEWAY_URL", value: parameter.modelConfig.llmGatewayUrl},
							{name: "LLM_GATEWAY_API_KEY", value: parameter.modelConfig.llmGatewayApiKey},
							// Platform-owned, not a parameter. "false": the agent presents its OWN ServiceAccount
							// identity to MCP servers, which mcpAccess grants match on. "true": it forwards the
							// user's token (main's behaviour, the passthrough the MCP spec forbids).
							{name: "PROPAGATE_CALLER_TOKEN", value: "{{ .Values.global.agentIdentity.propagateCallerToken }}"},
							// Observability env vars — mode-dependent
							{name: "OTEL_SERVICE_NAME", value: context.name},
							{name: "OTEL_TRACES_EXPORTER", value: "otlp"},
							if parameter.observability.mode == "centralized" {
								{name: "OTEL_EXPORTER_OTLP_ENDPOINT", value: "http://otel-collector.otel.svc.cluster.local:4318"}
							},
							if parameter.observability.mode == "decentralized" {
								{name: "OTEL_PYTHON_DISTRO", value: "aws_distro"}
							},
							if parameter.observability.mode == "decentralized" {
								{name: "OTEL_PYTHON_CONFIGURATOR", value: "aws_configurator"}
							},
							if parameter.observability.mode == "decentralized" {
								{name: "OTEL_EXPORTER_OTLP_PROTOCOL", value: "http/protobuf"}
							},
							if parameter.observability.mode == "decentralized" {
								{name: "OTEL_RESOURCE_ATTRIBUTES", value: "service.name=" + context.name}
							},
							if parameter.observability.mode == "decentralized" {
								{name: "AGENT_OBSERVABILITY_ENABLED", value: "true"}
							},
							// Langfuse: direct OTLP from agent (Strands StrandsTelemetry)
							{name: "LANGFUSE_PUBLIC_KEY", value: parameter.langfuse.publicKey},
							{name: "LANGFUSE_SECRET_KEY", value: parameter.langfuse.secretKey},
							{name: "LANGFUSE_BASE_URL", value: parameter.langfuse.baseUrl},
						], _memoryEnv, [
							if len(parameter.mcpServers) > 0 {
								{
									name:  "MCP_SERVER_NAMES"
									value: strings.Join([for s in parameter.mcpServers {s.name}], ",")
								}
							},
							for e in parameter.env {e},
						]])
						livenessProbe: {
							httpGet: {
								path: "/health"
								port: 8083
							}
							initialDelaySeconds: 10
							periodSeconds:       30
						}
						readinessProbe: {
							httpGet: {
								path: "/health"
								port: 8083
							}
							initialDelaySeconds: 5
							periodSeconds:       10
						}
						if parameter.resources != _|_ {
							resources: parameter.resources
						}

						// The microVM constrains what a compromised agent reaches on
						// the HOST; it does nothing about privileges inside the guest.
						// agent-sandbox-operator/values.yaml is explicit that the coder
						// sandboxes get their isolation from "the Kata micro-VM boundary
						// plus the restricted securityContext baked into the
						// SandboxTemplate pod spec" -- this component uses no
						// SandboxTemplate, so it supplies that second half itself.
						// Without this, sandbox: true would wrap a root container with
						// full capabilities in a VM and call it isolated.
						//
						// readOnlyRootFilesystem is deliberately NOT set: agent images
						// write to /tmp (model SDK caches, OTel buffers, Python
						// bytecode), so enabling it needs an emptyDir mount and
						// per-image verification. Tracked as a follow-up.
						//
						// runAsUser is REQUIRED here, not optional hardening.
						// runAsNonRoot does not ask "is the image non-root"; it asks the
						// kubelet to PROVE the user is non-root, and the kubelet cannot
						// prove that from a non-numeric `USER`. So an image that is
						// already non-root still fails to start with
						//   container has runAsNonRoot and image has non-numeric user
						//   (appuser), cannot verify user is non-root
						// This is not a corner case: the default image for this very
						// component (public.ecr.aws/z0a4o2j5/strands-agent) declares
						// `USER appuser`, so without an explicit numeric UID `sandbox: true`
						// could not start the platform's OWN default agent.
						//
						// Setting this cannot weaken the boundary: runAsNonRoot stays on and
						// rejects uid 0 at admission, so the parameter can only select WHICH
						// non-root user, never root.
						if parameter.sandbox {
							securityContext: {
								allowPrivilegeEscalation: false
								runAsNonRoot:             true
								runAsUser:                parameter.sandboxRunAsUser
								capabilities: drop: ["ALL"]
								seccompProfile: type: "RuntimeDefault"
							}
						}
					}]

					// Pod-level mirror of the container hardening above. Both levels
					// are set because they are enforced by different things:
					// Pod Security admission evaluates the POD's securityContext when
					// deciding whether a namespace at `restricted` admits the pod at
					// all, while the container context governs the running process.
					// Setting only the container would leave a restricted namespace
					// rejecting an otherwise-correct sandboxed agent.
					// runAsUser is mirrored too, so a `restricted` namespace evaluating the
					// POD context sees a complete, admissible spec rather than one that
					// satisfies runAsNonRoot only at the container level.
					if parameter.sandbox {
						securityContext: {
							runAsNonRoot: true
							runAsUser:    parameter.sandboxRunAsUser
							seccompProfile: type: "RuntimeDefault"
						}
					}
				}
			}
		}
	}

	outputs: {
		// Dedicated ServiceAccount — the agent's single identity anchor. The
		// gateway-identity and aws-service-identity traits attach their
		// capabilities to this SA (name == context.name).
		serviceAccount: {
			apiVersion: "v1"
			kind:       "ServiceAccount"
			metadata: {
				name:      context.name
				namespace: context.namespace
				labels: "app.kubernetes.io/name": context.name
			}
		}

		// Stable service (active)
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
					name: "a2a", port: 8083, targetPort: 8083, protocol: "TCP"
					appProtocol: "kgateway.dev/a2a"
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
					name: "a2a", port: 8083, targetPort: 8083, protocol: "TCP"
					appProtocol: "kgateway.dev/a2a"
				}]
				type: "ClusterIP"
			}
		}

		// Agent card ConfigMap
		agentCard: {
			apiVersion: "v1"
			kind:       "ConfigMap"
			metadata: {
				name:      context.name + "-card"
				namespace: context.namespace
				labels: {
					"app.kubernetes.io/name": context.name
					"agent.dev/type":         "agent-card"
				}
			}
			data: {
				name:        context.name
				description: parameter.description
				model:       parameter.modelConfig.modelId
			}
			if parameter.memory != _|_ {
				data: memoryProvider: parameter.memory.provider
			}
			if parameter.mcpServers != _|_ && len(parameter.mcpServers) > 0 {
				data: mcpServers: strings.Join([for s in parameter.mcpServers {s.name}], ",")
			}
		}

		// HTTPRoute for AgentGateway registration (optional)
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
								value: "/" + context.name
							}
						}]
						filters: [{
							type: "URLRewrite"
							urlRewrite: path: {
								type:               "ReplacePrefixMatch"
								replacePrefixMatch: "/"
							}
						}]
						backendRefs: [{
							name:      context.name + "-stable"
							port:      8083
							namespace: context.namespace
						}]
					}]
				}
			}
		}

	}

	parameter: {
		// Required fields
		description:   string
		systemMessage: string

		// Image
		image: *"public.ecr.aws/z0a4o2j5/strands-agent:v1.4.4-task-owner" | string

		// Optional fields with defaults
		// +usage=Number of replicas. Omitted means 1, and omitting it also lets an
		// autoscaler own the count.
		//
		// IMPORTANT: more than one replica is only safe when conversation state lives
		// outside the pod. Agents cache one Agent object per session in memory
		// (app/agent.py `_agents`), and there is no A2A session affinity available in
		// this stack, so a follow-up request that lands on another pod starts from
		// nothing. Verified: same contextId, one pod answered "Teal" and another said it
		// had no such information. Set a memory provider first (the agentcore-memory
		// trait, MEMORY_PROVIDER=agentcore), which attaches a session manager that
		// rehydrates state on any pod. The previous default of 3 was unsafe for exactly
		// this reason.
		replicas?: int

		// Blue-green deployment settings
		autoPromotionEnabled:  *true | bool
		autoPromotionSeconds:  *10 | int
		scaleDownDelaySeconds: *30 | int

		// Isolation
		// +usage=Run this agent inside a Kata microVM with a hardened securityContext.
		// Default false renders exactly today's pod. Which Kata VMM delivers the
		// isolation (kata-clh / kata-qemu / kata-fc) is a PLATFORM choice, set once
		// per cluster via global.sandboxRuntimeClass, and is deliberately NOT
		// expressible here: the VMM is ambient environment config, which a portable
		// OAM Application must never carry (.kiro/steering/oam-authoring.md §1).
		//
		// PRECONDITION: the image must run as non-root AND declare a NUMERIC uid, or
		// supply one via sandboxRunAsUser below. "not root" alone is not sufficient --
		// see that parameter for why. See docs/sandbox-agents/DESIGN.md.
		sandbox: *false | bool

		// +usage=Numeric UID a sandboxed agent's container runs as. Ignored unless sandbox is true.
		// REQUIRED, not cosmetic: runAsNonRoot does not ask "is this image non-root", it asks
		// the kubelet to PROVE the user is non-root, and the kubelet cannot prove that from a
		// named `USER`. Without a numeric uid even an already-non-root image fails with
		// "image has non-numeric user (...), cannot verify user is non-root".
		// The default matches this component's own default image
		// (public.ecr.aws/z0a4o2j5/strands-agent runs as appuser, uid 1000); override it for
		// an image that uses a different uid. This cannot be used to gain root -- runAsNonRoot
		// stays on and rejects uid 0 at admission, so it only selects WHICH non-root user.
		sandboxRunAsUser: *1000 | int

		// AgentGateway registration
		registerWithGateway: *true | bool
		gatewayNamespace:    *"agentgateway-system" | string

		// Model configuration
		// Gateway is Bifrost (OpenAI-compatible endpoint at /v1). llmGatewayApiKey
		// is the Bifrost virtual key (presented by the agent via the x-bf-vk header).
		modelConfig: {
			modelId:          *"claude-sonnet" | string
			llmGatewayUrl:    *"http://bifrost.bifrost.svc.cluster.local:8080/v1" | string
			llmGatewayApiKey: *"" | string
		}

		// Langfuse direct OTLP (Strands StrandsTelemetry sends traces directly)
		langfuse: {
			publicKey: *"" | string
			secretKey: *"" | string
			baseUrl:   *"" | string
		}

		// Observability mode
		// centralized: Agent → OTel Collector → Langfuse (traces) + AMP (metrics)
		// decentralized: Agent → ADOT → CloudWatch GenAI Console (traces + logs + metrics)
		observability: {
			mode: *"centralized" | "decentralized"
		}

		// Memory configuration — pluggable providers
		// mem0 providers: milvus, qdrant, opensearch, pgvector, redis, chroma, s3vectors
		// native provider: agentcore (uses Strands session manager directly, not mem0)
		memory?: {
			provider: "milvus" | "qdrant" | "opensearch" | "pgvector" | "redis" | "chroma" | "s3vectors" | "agentcore"
			config: {
				// mem0 vector store providers
				// milvus:     url, collectionName
				// qdrant:     url, apiKey?
				// opensearch: url, indexName
				// pgvector:   host, port, user, password, dbName
				// redis:      url, password?
				// chroma:     host, port
				// s3vectors:  bucket, region
				// agentcore:  memoryId, region
				{[string]: string}
			}
		}

		// MCP servers
		mcpServers: *[] | [...{
			name: string
		}]

		// Additional environment variables
		env: *[] | [...{
			name:  string
			value: string
		}]

		// Resource limits
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
	}
}
