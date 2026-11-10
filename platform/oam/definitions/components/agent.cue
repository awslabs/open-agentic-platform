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

	// PLATFORM-OWNED confidential runtime, a CUE local for the same reason as the
	// one above. Separate from _sandboxRuntimeClass because the two are different
	// security boundaries rather than two spellings of one: confidential-sandbox
	// also encrypts the overlay, denies exec and stdin from the parent instance,
	// and measures the pod's Cedar policy into attestation register PCR17.
	//
	// EMPTY IS THE CORRECT VALUE on a cluster that cannot offer it -- a Kata
	// cluster has no confidential equivalent at all -- and the guard below then
	// rejects sandboxMode: confidential instead of substituting the isolated class.
	_confidentialSandboxRuntimeClass: "{{ .Values.global.confidentialSandboxRuntimeClass }}"

	// PLATFORM-OWNED switch for per-session isolation, a string because it is
	// substituted into CUE as text. "true" only on a cluster that actually has the
	// pieces: the agent-sandbox operator (which is the ONLY source of the
	// SandboxTemplate / SandboxWarmPool / SandboxClaim CRDs) and an agent image
	// that claims a sandbox per session.
	//
	// This is not belt-and-braces over the CRDs being absent. Without the operator
	// the agent renders perfectly, deploys, serves traffic and silently shares one
	// process between every user -- the exact failure #99 exists to remove, and
	// invisible precisely because the agent still works. The guard turns it into an
	// admission error naming the missing prerequisite.
	_sessionIsolationEnabled: "{{ .Values.global.sessionIsolationEnabled }}"

	// The class the pod actually selects: the trust model picks which platform
	// value is read. Written as an indexed list rather than two `if` blocks on one
	// field because the two branches would otherwise unify, and a cluster where
	// the isolated and confidential classes differ (every cluster) would render
	// bottom.
	_isolationClass: [
		if parameter.sandboxMode == "confidential" {_confidentialSandboxRuntimeClass},
		_sandboxRuntimeClass,
	][0]

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
	if parameter.sandbox && parameter.sandboxMode == "isolated" && _sandboxRuntimeClass == "" {
		_|_ // "sandbox: true requires global.sandboxRuntimeClass on the
		//  oam-agent-components chart; this cluster has no isolation runtime
		//  configured."
	}

	// Same fail-closed rule for the confidential model, with its own message
	// because the remedy is different: this cluster may well have a working
	// isolated class and still be unable to offer confidential (any Kata cluster
	// is). Substituting the isolated class here would be the worst outcome
	// available -- the Application would deploy, the pod would run, and the
	// confidentiality the developer asked for would simply not exist.
	//
	// The two strings conflict on purpose. KubeVela prints both sides of a failed
	// unification, so the error carries the explanation; a bare _|_ reports only
	// "explicit error in source" and leaves the developer guessing which of the
	// two classes is missing.
	if parameter.sandbox && parameter.sandboxMode == "confidential" && _confidentialSandboxRuntimeClass == "" {
		_confidentialAvailable: "set" & "sandboxMode: confidential needs global.confidentialSandboxRuntimeClass on the oam-agent-components chart (cluster-secret annotation confidential_sandbox_runtime_class). This cluster offers no confidential isolation runtime: Kata has no equivalent, and EKS Nitro Sandboxes need Kubernetes 1.36+. Use sandboxMode: isolated, or deploy to a cluster that has it."
	}

	// FAIL CLOSED on confidential WITHOUT a Cedar policy, which is the subtlest
	// way to end up with a pod that is confidential in name only.
	//
	// A sandbox with no policy annotation falls back to a PERMISSIVE built-in
	// default: every container admitted, and exec, stdin and process listing all
	// allowed from the parent instance. The memory boundary would still hold, so
	// the pod runs and looks right, while the node operator this mode exists to
	// defend against can read the workload's data by exec'ing into it. Nothing
	// anywhere would report a problem -- the same silent shape as an unconfigured
	// runtime class, and the reason that case already fails closed.
	//
	// It also makes attestation meaningless: PCR17 measures the policy, so with
	// no policy there is nothing for a KMS key policy to pin.
	//
	// Generate it from the FINAL rendered pod spec with
	// scripts/nitro/gen-cedar-policy.sh, and regenerate whenever a pinned field
	// changes -- including a new image digest.
	if parameter.sandbox && parameter.sandboxMode == "confidential" && parameter.cedarPolicy == "" {
		_cedarPolicyRequired: "set" & "sandboxMode: confidential requires cedarPolicy: a base64 Cedar policy generated from this agent's rendered pod spec. Without one the sandbox applies a permissive built-in default that allows exec and stdin from the parent instance, so the node operator can still read the workload -- and PCR17 attests nothing. Generate it with scripts/nitro/gen-cedar-policy.sh, or use sandboxMode: isolated."
	}

	// FAIL CLOSED when per-session isolation is asked for and the platform cannot
	// deliver it. See _sessionIsolationEnabled above for why silence is the wrong
	// answer here: the agent would work, and every user would share one process.
	if parameter.isolation != "shared" && _sessionIsolationEnabled != "true" {
		_sessionIsolationUnavailable: "set" & "isolation: session needs per-session sandboxing on this cluster: enable the agent_sandbox addon (it installs the SandboxTemplate / SandboxWarmPool / SandboxClaim CRDs) and set global.sessionIsolationEnabled: \"true\" on the oam-agent-components chart. Without it this agent would deploy, serve traffic, and silently share one process between every user. Use isolation: shared, which is what it would have been."
	}

	// FAIL CLOSED on a tier that names no pool. isolation: session works by
	// CLAIMING from a sandbox declared elsewhere --
	// a `type: sandbox` component, usually in the same Application. There is no
	// default pool to fall back on, and an agent that claims from nowhere would run
	// in its own shared process: the shared behaviour, under a name that
	// promises otherwise.
	//
	// Deliberately a NAME rather than a rendered reference: KubeVela has no
	// cross-component typed reference, and the convention is the same one the rest
	// of this chart uses (steering §3). A typo therefore fails at claim time, not
	// at render time, which is why the name is also published in the agent card for
	// an operator to check.
	if parameter.isolation != "shared" && parameter.sandboxTemplate == "" {
		_sandboxTemplateRequired: "set" & "isolation: session requires sandboxTemplate: the name of a `type: sandbox` component to claim per-session instances from. Without it the agent would run its tools in its own process -- the shared behaviour under a name that promises isolation."
	}

	// REJECT `tools` WITH AN EXPLANATION, rather than by omission.
	//
	// #99 proposes three tiers and names `tools` as its recommended first step,
	// so people will copy that word out of the issue. Leaving it out of the
	// disjunction above does reject it -- as
	//   outputs.agentCard.data.isolation: 2 errors in empty disjunction
	// which tells the developer nothing about why or what to do instead. The
	// value is accepted by the type and refused here so the error can carry the
	// reasoning.
	//
	// WHY IT IS REFUSED AT ALL, since the capability it names is real and wanted:
	// this is the wrong layer to express it from. `shared` and `session` answer
	// HOW MANY AGENT INSTANCES exist per user, a property of the agent. "Where
	// does code execution run" is a property of the TOOL SERVER -- an agent just
	// has an MCP server wired, and whether that server executes in AgentCore or
	// in a claimed sandbox is the server's own configuration. A tier here would
	// duplicate a decision that belongs one layer down.
	//
	// It is also redundant either way it could be wired: AgentCore already
	// isolates each session off-cluster, and with an on-EKS backend the
	// per-session sandbox IS the tool backend, so the "tier" is just
	// code-interpreter-mcp pointed at a `type: sandbox` pool.
	if parameter.isolation == "tools" {
		_toolsTierNotSupported: "set" & "isolation: tools is not supported, deliberately rather than as missing work. shared and session answer how many AGENT instances a user gets; where TOOLS execute is the tool server's business -- an agent just has an MCP server wired, and whether that server runs code in AgentCore or in a claimed sandbox is its own configuration. It is also redundant either way: AgentCore already isolates each session off-cluster, and with an on-EKS backend the per-session sandbox IS the tool backend. For per-session tool isolation: declare a `type: sandbox` pool, point an MCP server at it, and leave this agent on isolation: shared. See docs/sandbox-agents/NITRO-DESIGN.md section 2.2."
	}

	// Per-session isolation wiring, handed to the agent process as env.
	//
	// The platform renders the DECLARATION -- which pool to claim from, in which
	// namespace, and how to key a session -- and grants the RBAC to claim. Creating
	// and releasing a claim is the agent's own work at request time, because only
	// the agent knows when a session starts, and only it sees the caller's subject
	// and the A2A contextId that together identify one.
	//
	// Empty for isolation: shared, so a shared agent's env is byte-identical to
	// today's.
	let _isolationEnv = [
		if parameter.isolation != "shared" {
			{
				name:  "AGENT_ISOLATION"
				value: parameter.isolation
			}
		},
		if parameter.isolation != "shared" {
			{
				name:  "SANDBOX_WARM_POOL"
				value: parameter.sandboxTemplate
			}
		},
		if parameter.isolation != "shared" {
			{
				// The claim must land in the agent's own namespace: the RBAC below is a
				// Role, not a ClusterRole, so a claim anywhere else is denied. Passed
				// explicitly because the agent is not otherwise told its namespace.
				name:  "SANDBOX_NAMESPACE"
				value: context.namespace
			}
		},
		if parameter.isolation != "shared" {
			{
				name:  "AGENT_SESSION_KEY"
				value: parameter.sessionKey
			}
		},
	]


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
				metadata: {
					labels: {
						"app.kubernetes.io/name": context.name
					}

					// Nitro Sandbox pod annotations. They belong on the POD TEMPLATE,
					// never on the Rollout's own metadata: Kubernetes does not copy a
					// controller's annotations into the pods it creates, so a
					// sandbox.amazonaws.com/ key placed there is accepted and then
					// silently ignored. The EKS user guide calls this out for
					// Deployments; a Rollout has the same shape.
					//
					// Emitted only when a policy is supplied, so an isolated agent's
					// output is unchanged. The Cedar policy is OPTIONAL in isolated mode
					// (the sandbox applies a permissive built-in default) and MANDATORY
					// in confidential mode, which the guard above enforces.
					//
					// Attaching it does NOT invalidate the policy it carries: a
					// confidential policy pins container admission fields -- image
					// digest, args, env, mounts, capabilities, security settings -- not
					// pod annotations, so a policy generated from the rendered pod
					// template still matches the pod the annotation lands on.
					if parameter.sandbox && parameter.cedarPolicy != "" {
						annotations: "sandbox.amazonaws.com/policy": parameter.cedarPolicy
					}

					// Per-pod shared-resource throttles. A Nitro Sandbox gets a
					// default budget for the node resources every sandbox shares,
					// and an agent is an unusually heavy consumer of two of them:
					// it holds outbound connections to the model gateway and to
					// every MCP server it is wired to, and it resolves those names
					// repeatedly. The defaults (4096 simultaneous outbound
					// connections per IP family; 100 DNS queries per second) are
					// generous, but they are per POD and invisible -- exceeding
					// them shows up as dropped connections and resolution
					// failures inside the guest, not as a Kubernetes event. These
					// are the dials, declared so a fan-out agent does not have to
					// leave the abstraction to reach them.
					//
					// Nitro-specific, and harmless on Kata: an unknown annotation
					// is ignored, so the same Application stays portable.
					if parameter.sandbox && parameter.sandboxConntrackLimit != _|_ {
						annotations: "sandbox.amazonaws.com/conntrack-limit": "\(parameter.sandboxConntrackLimit)"
					}
					if parameter.sandbox && parameter.sandboxDnsQueriesPerSecond != _|_ {
						annotations: "sandbox.amazonaws.com/dns-packets-per-second": "\(parameter.sandboxDnsQueriesPerSecond)"
					}
				}
				spec: {
					serviceAccountName: context.name

					// Isolation: this single field is the whole node-placement
					// mechanism. Kubernetes' built-in RuntimeClass admission
					// controller force-merges the class's scheduling.nodeSelector
					// and scheduling.tolerations onto the pod, and applies its
					// overhead.podFixed for scheduling and kubelet accounting --
					// so nothing here needs to know about the sandbox node pool,
					// its taint, or the microVM's memory cost. That holds for both
					// runtimes: the Nitro classes carry
					// eks.amazonaws.com/nitro-sandbox as their nodeSelector and
					// charge memory plus one nitro-sandbox slot as overhead, the Kata
					// classes carry katacontainers.io/kata-runtime and a matching
					// toleration, and this pod spec names neither.
					if parameter.sandbox {
						runtimeClassName: _isolationClass
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
							if parameter.modelConfig.temperature != _|_ {
								{name: "MODEL_TEMPERATURE", value: "\(parameter.modelConfig.temperature)"}
							},
							{name: "SYSTEM_PROMPT", value: parameter.systemMessage},
							{name: "PORT", value: "8083"},
							{name: "LLM_GATEWAY_URL", value: parameter.modelConfig.llmGatewayUrl},
							{name: "LLM_GATEWAY_API_KEY", value: parameter.modelConfig.llmGatewayApiKey},
							// Platform-owned, not a parameter. "false": the agent presents its OWN ServiceAccount
							// identity to MCP servers, which mcpAccess grants match on. "true": it forwards the
							// user's token (main's behaviour, the passthrough the MCP spec forbids).
							{name: "PROPAGATE_CALLER_TOKEN", value: "{{ .Values.global.agentIdentity.propagateCallerToken }}"},
							// Platform-owned, not parameters: together these scope the agent's
							// Langfuse trace tag to one cluster, as agent:<cluster>/<ns>/<name>.
							// Every cluster's traces land in the one hub Langfuse project, so
							// without the cluster the same Application on dev and prod would
							// share a tag -- one evaluation rule would match both, and their
							// scores would be one indistinguishable series.
							// The agent is not told its own namespace otherwise, and the tag
							// needs it.
							{name: "CLUSTER_NAME", value: "{{ .Values.global.clusterName }}"},
							{name: "AGENT_NAMESPACE", value: context.namespace},
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
						], _memoryEnv, _isolationEnv, [
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
								path: "/ready"
								port: 8083
							}
							initialDelaySeconds: 5
							periodSeconds:       10
							// /ready connects any not-yet-connected MCP server
							// (blocking, in a worker thread), so allow headroom.
							timeoutSeconds: 10
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
			// The isolation tier and the pool it claims from, published for the
			// same reason the model and memory provider are: an operator looking at
			// one ConfigMap can tell what a deployed agent actually does. It matters
			// more here than for the others, because sandboxTemplate is a NAME with
			// no render-time check -- a typo surfaces as claims that fail, and this
			// is where you look to see what was asked for.
			//
			// Omitted for shared, so a shared agent's card is unchanged.
			if parameter.isolation != "shared" {
				data: isolation: parameter.isolation
				data: sandboxWarmPool: parameter.sandboxTemplate
				data: sessionKey: parameter.sessionKey
			}

			if parameter.mcpServers != _|_ && len(parameter.mcpServers) > 0 {
				data: mcpServers: strings.Join([for s in parameter.mcpServers {s.name}], ",")
			}
		}

		// Evaluation declaration. One ConfigMap naming WHAT this agent wants
		// evaluated; the agent-evaluation addon's reconciler owns everything else
		// (D4). There is no account, region, endpoint or key in it.
		//
		// Because this is a KubeVela output, deleting the Application or setting
		// enabled: false deletes it, and the reconciler then removes the agent
		// from its evaluation rules on the next pass. That is what handles
		// cleanup with no finalizer.
		//
		// RESOLVE THE OPTIONAL FIELD BEHIND ITS OWN GUARD, then branch on a plain
		// bool. `if parameter.evaluation != _|_ && parameter.evaluation.enabled`
		// reads as a short-circuit and is not one: CUE still resolves
		// `parameter.evaluation.enabled` while `evaluation` is unset, and marshalling
		// `outputs` then fails for EVERY agent that does not set the block --
		//   cannot reference optional field: evaluation
		// The webhook denies such an Application outright; one admitted under an
		// older definition fails its workflow step instead, while ArgoCD still
		// reports Synced/Healthy because the Application resource itself is fine
		// and only the Deployment is missing.
		//
		// This is why `evaluation` needs the extra hop and the two older patterns
		// above do not: `mcpServers` is `*[] | [...]`, always concrete, and
		// `parameter.memory.config != _|_` only tests existence rather than reading
		// a value. `evaluation?` is optional with no default AND its `enabled` is
		// read as the condition, which is the combination that breaks.
		_evaluationEnabled: *false | bool
		if parameter.evaluation != _|_ {
			_evaluationEnabled: parameter.evaluation.enabled
		}
		// FAIL CLOSED ON AN UNSUPPORTED COMBINATION, rather than admitting an
		// Application that can never be scored.
		//
		// Phase 1 evaluates traces in Langfuse, which only receives them in
		// `centralized` observability mode. A `decentralized` agent sends its
		// traces to CloudWatch instead, so an evaluation rule has nothing to
		// match: the Application is healthy, the ConfigMap is correct, the rule
		// exists, and no score is ever produced. That is the same silent shape as
		// the trace-tag drift, and it belongs at admission where the developer
		// sees it immediately.
		//
		// Unifying with the one supported value is what rejects it: for any other
		// mode this is bottom, so the render fails and the webhook denies the
		// Application. The annotation key below is named for the error message it
		// produces. Remove this when Phase 2 (AgentCore Evaluations) can score
		// the decentralized path.
		if _evaluationEnabled {
			evaluationSpec: {
				apiVersion: "v1"
				kind:       "ConfigMap"
				metadata: {
					name:      context.name + "-evaluation"
					namespace: context.namespace
					labels: {
						// The reconciler lists on this label alone, so it must be
						// present for the agent to be evaluated at all.
						"evaluation.oap.io/agent": context.name
						"app.kubernetes.io/name":  context.name
					}
					annotations: {
						// Carries the unification, so the constraint is part of the
						// rendered output and cannot be skipped by lazy evaluation
						// the way a hidden field could.
						"evaluation.oap.io/observability-mode": "centralized" & parameter.observability.mode
					}
				}
				data: {
					agent: context.name
					// NO `backend` KEY. It used to carry observability.mode so a
					// future AgentCore path could branch on it, but nothing ever
					// read it and the mode is now constrained to `centralized`
					// above -- so it could only ever have held one value. The
					// backend is the reconciler's own concern, selected by which
					// implementation the addon runs, not something an agent
					// declares.
					//
					// The agent's model alias, so reports can compare quality by model.
					model:      parameter.modelConfig.modelId
					strictness: parameter.evaluation.strictness
					coverage:   parameter.evaluation.coverage
					// Names and per-evaluator overrides exactly as given. The
					// reconciler resolves them against the platform catalog, because
					// CUE renders without cluster reads and cannot see it.
					evaluators: json.Marshal(parameter.evaluation.evaluators)
					if parameter.evaluation.sampling != _|_ {
						sampling: "\(parameter.evaluation.sampling)"
					}
					// Absent means "use the platform default judge".
					if parameter.evaluation.judge != _|_ {
						judge: parameter.evaluation.judge.modelId
					}
				}
			}
		}

		// Permission to claim a per-session sandbox, granted only to the tiers that
		// claim one. A Role and not a ClusterRole: a claim outside the agent's own
		// namespace is denied, which is what keeps one tenant's agent from claiming
		// instances out of another namespace's pool.
		//
		// Scoped to the three verbs the lifecycle needs and nothing else (steering
		// §4). Notably absent: any write verb on `sandboxes` -- the agent claims, and
		// the OPERATOR creates and deletes the instance. An agent that could delete a
		// Sandbox directly could destroy another session's instance out from under
		// it, and `update` on a claim would let it extend its own TTL past the
		// lifecycle the sandbox component declared.
		//
		// delete IS granted, because releasing a session's sandbox as soon as the
		// session ends is how the per-session tiers stay affordable: waiting for the
		// idle TTL on every abandoned session is the difference between a warm pool
		// that recycles and one that is permanently exhausted.
		if parameter.isolation != "shared" {
			sandboxClaimerRole: {
				apiVersion: "rbac.authorization.k8s.io/v1"
				kind:       "Role"
				metadata: {
					name:      context.name + "-sandbox-claimer"
					namespace: context.namespace
					labels: "app.kubernetes.io/name": context.name
				}
				rules: [{
					apiGroups: ["extensions.agents.x-k8s.io"]
					resources: ["sandboxclaims"]
					verbs: ["create", "get", "list", "watch", "delete"]
				}, {
					// Read-only on the instances themselves: the agent needs the bound
					// Sandbox's status to find out when it is ready and where to reach
					// it. Creating and deleting them is the operator's job.
					apiGroups: ["agents.x-k8s.io"]
					resources: ["sandboxes"]
					verbs: ["get", "list", "watch"]
				}]
			}

			sandboxClaimerRoleBinding: {
				apiVersion: "rbac.authorization.k8s.io/v1"
				kind:       "RoleBinding"
				metadata: {
					name:      context.name + "-sandbox-claimer"
					namespace: context.namespace
					labels: "app.kubernetes.io/name": context.name
				}
				// Bound to the agent's own ServiceAccount -- the single identity anchor
				// this component already owns, which gateway-identity and
				// aws-service-identity also attach to. No second identity to keep in
				// step.
				roleRef: {
					apiGroup: "rbac.authorization.k8s.io"
					kind:     "Role"
					name:     context.name + "-sandbox-claimer"
				}
				subjects: [{
					kind:      "ServiceAccount"
					name:      context.name
					namespace: context.namespace
				}]
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

		// +usage=Agent container image. Defaults to the PLATFORM's agent image, so
		// an ordinary Application never names one ("imageless").
		//
		// The default is a platform value rather than a literal pinned here, for
		// the same reason region and account are (.kiro/steering/oam-authoring.md
		// §1): which registry a cluster can pull from is ambient environment
		// config. A cluster that mirrors the agent image into its own ECR sets
		// global.agentImage once on the oam-agent-components chart, instead of
		// every Application overriding `image` or the CUE default being edited to
		// name one account's private registry.
		//
		// This matters for continuous agent evaluation specifically: the scoped
		// `agent:<cluster>/<ns>/<name>` trace tag that evaluation rules filter on
		// is emitted by the IMAGE. An image predating that tag is configured for
		// evaluation and silently never scored, so a cluster needs to be able to
		// point at an image that has it without touching any Application.
		image: *"{{ .Values.global.agentImage }}" | string

		// Optional fields with defaults
		// +usage=Number of replicas. Omitted means 1, and omitting it also lets an
		// autoscaler own the count.
		//
		// IMPORTANT: more than one replica is only safe when conversation state lives
		// outside the pod. Agents cache one Agent object per session in memory
		// (app/agent.py `_agents`), and there is no A2A session affinity available in
		// this stack, so a follow-up request that lands on another pod starts from
		// nothing. Verified: same contextId, one pod answered "Teal" and another said it
		// had no such information. Set a memory provider first (the agentcore-memory component,
		// trait, MEMORY_PROVIDER=agentcore), which attaches a session manager that
		// rehydrates state on any pod. The previous default of 3 was unsafe for exactly
		// this reason. A2A tasks (tasks/get, tasks/cancel, tasks/resubscribe) stay
		// pod-local even with memory, so an agent whose callers use them needs one replica.
		replicas?: int

		// Blue-green deployment settings
		autoPromotionEnabled:  *true | bool
		autoPromotionSeconds:  *10 | int
		scaleDownDelaySeconds: *30 | int

		// Isolation
		// +usage=Run this agent inside a hardware-isolated microVM (its own kernel,
		// its own memory) with a hardened securityContext, instead of a
		// shared-kernel container on a general node pool.
		// Default false renders exactly today's pod.
		//
		// WHICH isolation runtime delivers it -- AWS Nitro Sandboxes
		// (isolated-sandbox, the platform default) or Kata (kata-clh / kata-qemu /
		// kata-fc) -- is a PLATFORM choice, set per cluster via
		// global.sandboxRuntimeClass, and is deliberately NOT expressible here: the
		// runtime is ambient environment config, which a portable OAM Application
		// must never carry (.kiro/steering/oam-authoring.md §1). The developer-facing
		// choice is the TRUST MODEL, below.
		//
		// PRECONDITION: the image must run as non-root AND declare a NUMERIC uid, or
		// supply one via sandboxRunAsUser below. "not root" alone is not sufficient --
		// see that parameter for why. See docs/sandbox-agents/NITRO-DESIGN.md.
		sandbox: *false | bool

		// +usage=Trust model for the sandbox. Ignored unless sandbox is true.
		//
		//   isolated     (default) The node is TRUSTED; the workload is not. Protects
		//                the node and every other pod from a compromised agent. The
		//                platform keeps full observability: kubectl exec, stdin, logs
		//                and stats all work.
		//   confidential The NODE is untrusted too. A cluster administrator cannot
		//                read the agent's memory, the overlay filesystem is
		//                encrypted, and exec and stdin are DENIED from the host. The
		//                guest attests its measurements to AWS KMS, so a secret can
		//                be released only to the exact code and policy you expect.
		//
		// This IS portable and so it IS a developer parameter: it states a property
		// of the workload ("my data must survive an untrusted node operator"), not a
		// property of the cluster. The platform still owns the class each model maps
		// to.
		//
		// confidential REQUIRES cedarPolicy below, and a cluster that configures
		// global.confidentialSandboxRuntimeClass. Kata cannot provide it: on a
		// Kata-only cluster this fails the render rather than handing back an
		// ordinary microVM that cannot attest anything.
		sandboxMode: *"isolated" | "confidential"

		// +usage=Base64-encoded Cedar policy for the sandbox, attached to the pod as
		// the sandbox.amazonaws.com/policy annotation. Ignored unless sandbox is true.
		//
		// REQUIRED for sandboxMode: confidential, OPTIONAL for isolated. The policy is
		// what the sandbox enforces from INSIDE the guest -- which images may load,
		// which containers may run, whether exec, stdin and log reads are permitted,
		// and which mounts are allowed -- so it holds even when the parent instance is
		// compromised. In confidential mode it is also measured into attestation
		// register PCR17, which is what a KMS key policy pins.
		//
		// Generate it with scripts/nitro/gen-cedar-policy.sh, which renders THIS
		// Application, extracts the pod template and runs eks-nitro-sandbox-genpolicy
		// over it. It must be generated from the FINAL spec, after every mutating
		// webhook, and regenerated whenever a pinned field changes -- a new image
		// digest alone is enough. A stale policy does not degrade: the pod is denied.
		//
		// Base64, so it is also safe to paste into YAML; it stays a single line and
		// carries no characters that need quoting.
		//
		// A useful isolated-mode case: generate with --deny-exec to stop operators
		// shelling into an agent that handles regulated data, without taking on
		// confidential mode's attestation and image-pinning work.
		cedarPolicy: *"" | string

		// +usage=Maximum simultaneous outbound connections per IP family for a
		// sandboxed agent (Nitro default 4096, range 64-65536). Ignored unless sandbox
		// is true. Raise it for an agent that fans out to many MCP servers or holds
		// long-lived streams; exceeding the limit appears as dropped connections
		// inside the guest, with nothing reported at the Kubernetes layer.
		sandboxConntrackLimit?: int & >=64 & <=65536

		// +usage=Maximum DNS queries per second for a sandboxed agent (Nitro default
		// 100, maximum 512). Ignored unless sandbox is true. An agent that resolves
		// its gateway and MCP endpoints per request can exceed the default under load
		// and see resolution failures rather than a throttling signal.
		sandboxDnsQueriesPerSecond?: int & >=1 & <=512

		// Per-user, per-session isolation
		// +usage=How much of this agent is instantiated PER USER SESSION rather than
		// shared by every caller. Declared once by the developer; users never submit
		// OAM of their own.
		//
		//   shared  (default) One agent process for everyone. Memory and context are
		//           already isolated per caller, so this is right for Q&A, retrieval
		//           and read-only tools. Lowest cost, and byte-identical to the
		//           output before this parameter existed.
		//
		// THERE IS DELIBERATELY NO `tools` TIER, though #99 proposes one ("shared
		// agent loop, per-session code execution"). That capability is real and
		// wanted; this is the wrong layer to express it from.
		//
		// `shared` and `session` answer HOW MANY AGENT INSTANCES exist per user,
		// which is a property of the agent -- so this component owns it. "Where
		// does code execution run" is a property of the TOOL SERVER: an agent
		// just has an MCP server wired, and whether that server executes code in
		// AgentCore or in a claimed sandbox is the server's own configuration. A
		// field here would duplicate a decision that belongs one layer down.
		//
		// It is also redundant either way it is wired. With the AgentCore backend
		// each session is already isolated off-cluster, so the tier would add
		// nothing. With an on-EKS backend the per-session sandbox IS the tool
		// backend, so the tier would just be code-interpreter-mcp pointed at a
		// `type: sandbox` pool -- one config value there, plus a pool the
		// developer declares anyway.
		//
		// To get per-session TOOL isolation: declare a `type: sandbox` pool and
		// point an MCP server at it, leaving the agent on `shared`.
		//
		// The word `tools` IS accepted by this field and then refused at
		// admission with that explanation, because #99 names it and a developer
		// copying it deserves a message rather than a CUE disjunction error.
		//   session A dedicated sandbox instance per (agent, user, session), so two
		//           users -- or two sessions of one user -- never share a process, a
		//           filesystem or a VM. Highest cost. For regulated or multi-tenant
		//           work, long-lived per-user state, or an AgentCore-Runtime-equivalent
		//           guarantee on EKS.
		//
		// blue-green and replicas keep their meaning in every tier, because the agent
		// stays an Argo Rollout in both tiers. In session it is the front
		// door that resolves a session key and routes to that session's instance; the
		// instance is a `type: sandbox` component, not a replica of this one. (That
		// answers #99's open question 5: nothing has to be rejected.)
		//
		// Both non-shared tiers need the agent_sandbox addon and an agent image that
		// claims sandboxes. Asking for them without that is an admission error, not a
		// silent downgrade.
		isolation: *"shared" | "session" | "tools"

		// +usage=Name of the `type: sandbox` component this agent claims per-session
		// instances from. Required for isolation: session, ignored for shared.
		//
		// Must name a sandbox in the SAME NAMESPACE: the claim RBAC this component
		// renders is a Role, so a claim anywhere else is denied.
		sandboxTemplate: *"" | string

		// +usage=What identifies one session, for the per-session tiers.
		//
		//   subject+context (default) The caller's gateway subject AND the A2A
		//                   contextId. Two conversations of one user get two
		//                   sandboxes; a client that sends no context falls back to
		//                   the subject, so it still never shares with another user.
		//   subject         One sandbox per user, shared by all their conversations.
		//                   Cheaper, and enough when the isolation boundary you care
		//                   about is the tenant.
		//   context         One sandbox per conversation, ignoring who is calling.
		//                   For anonymous or service-to-service callers where the
		//                   subject is not a tenant.
		//
		// (This is #99's open question 1, answered as a declaration rather than a
		// platform convention: the right key depends on what the agent's tenants are,
		// which only its developer knows.)
		sessionKey: *"subject+context" | "subject" | "context"

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
			// Omitted unless set; newer models reject non-default values.
			temperature?: number & >=0 & <=1
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

		// +usage=Online evaluation of this agent's live traces by an LLM judge.
		// Requires the agent-evaluation addon on the cluster, and
		// observability.mode: centralized -- an Application that turns evaluation
		// on with decentralized observability is REJECTED at admission rather
		// than admitted and never scored, because the judge reads traces from
		// Langfuse and the decentralized path sends them to CloudWatch instead.
		// Scoring the decentralized path is Phase 2 (AgentCore Evaluations).
		//
		// The developer chooses WHICH evaluators run, HOW STRICT they are, HOW MANY
		// traces are scored and optionally WHICH judge. What each evaluator means --
		// its prompt, scale and thresholds -- is platform-owned, so judge prompts
		// stay reviewed in one place and every agent is held to the same definition
		// of "safe" or "helpful".
		evaluation?: {
			// +usage=Turn evaluation on for this agent.
			enabled: *false | bool
			// +usage=Judge model, as a Bifrost model alias. Defaults to the platform
			// judge. Must be one of the aliases the platform allows; an unknown one is
			// reported as a Kubernetes Event on this agent's evaluation ConfigMap and
			// no rule is created, rather than silently falling back to the default.
			judge?: modelId: string
			// +usage=How high a score must be to pass: low, medium or high. The
			// platform catalog defines the threshold for each evaluator at each level,
			// so `high` is stricter for safety than for helpfulness.
			// Costs nothing: it changes the reported threshold, not the judge calls.
			strictness: *"medium" | "low" | "high"
			// +usage=How many traces are scored: low, medium or high. The platform
			// defines the sampling rate for each level. Each scored trace costs one
			// judge call per evaluator, so this is the cost dial.
			coverage: *"medium" | "low" | "high"
			// +usage=Exact fraction of traces scored, 0-1. Overrides coverage.
			// Prefer coverage: the three preset rates group into far fewer Langfuse
			// rules, and the project has a cap on them.
			sampling?: number & >0 & <=1
			// +usage=Evaluators from the platform catalog: a name, or
			// {name, strictness} to override strictness for one evaluator. Names are
			// checked by the reconciler, which owns the catalog: CUE renders without
			// cluster reads, so it cannot see catalog entries teams add.
			//
			// A bare name is a platform catalog entry. The <namespace>/<name> form is
			// reserved for team-defined evaluators (D4, "Planned follow-up") and is
			// not accepted yet, so widening #EvaluatorName is part of that follow-up.
			evaluators: *["goal-completion", "helpfulness"] | [...(#EvaluatorName | {
				name:        #EvaluatorName
				strictness?: "low" | "medium" | "high"
			})]
		}

		// Lowercase alphanumeric with internal hyphens. `--` is excluded because it
		// separates an evaluator from its judge alias in the Langfuse object name
		// (<evaluator>--<alias>), so a catalog name containing it would be ambiguous.
		#EvaluatorName: =~"^[a-z0-9]([a-z0-9]|-[a-z0-9])*$"

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
				// agentcore:  memoryId (from the agentcore-memory component), region,
				//             retrievalTopK and retrievalRelevanceScore (numbers written
				//             as strings, e.g. "5" and "0.3"; an unparsable value falls
				//             back to the default, 10 and 0.2, with a warning)
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
