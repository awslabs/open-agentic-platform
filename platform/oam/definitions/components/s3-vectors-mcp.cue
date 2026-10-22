import (
	"encoding/json"
	"list"
	"strings"
)

// s3-vectors-mcp ComponentDefinition
//
// A read-only MCP server that answers semantic-search queries against ONE existing
// Amazon S3 Vectors index, registered with AgentGateway at /mcp/<name>. The vector
// bucket and index are assumed to exist: an ingestion pipeline outside OAP creates
// them and writes the vectors. This component only reads.
//
// It is mcp-server specialised for S3 Vectors. The typed parameters (vectorBucket,
// index, embeddingModel) drive both the server's configuration and an IAM policy
// scoped to exactly that index and model, so a developer never writes either.
//
// Identity follows the platform split: this component emits the PERMISSION as an
// IAM Policy named <appName>-<component>-iam-policy; the aws-service-identity trait
// owns the IDENTITY (Pod Identity role) and attaches the policy when it is given this
// component's own name:
//
//   traits:
//     - type: aws-service-identity
//       properties:
//         accessFor: [<this component's name>]
//
// Without that trait the pod has no AWS credentials and never becomes ready; its
// /readyz reason names the missing accessFor entry.
//
// The workload, ServiceAccount, Services, AgentgatewayBackend and HTTPRoute are a
// copy of mcp-server.cue's. `vela def render` renders each definition from a
// self-contained file and does not resolve local CUE imports (see the note in
// mcp-server.cue), so sharing them would need a cluster-registered package. Keep the
// two in step when changing either.
"s3-vectors-mcp": {
	alias:       ""
	annotations: {}
	attributes: {
		workload: definition: {
			apiVersion: "argoproj.io/v1alpha1"
			kind:       "Rollout"
		}
		status: healthPolicy: #"isHealth: (context.output.status.phase != _|_) && (context.output.status.phase == "Healthy")"#
	}
	description: "Read-only MCP server for semantic search over one existing Amazon S3 Vectors index, with an IAM policy scoped to that index"
	labels: {}
	type: "component"
}

template: {
	let _port = 8000

	// Index ARN. The partition is "aws", as in every other definition here.
	let _indexArn = "arn:aws:s3vectors:\(parameter.region):{{ .Values.global.awsAccountId }}:bucket/\(parameter.vectorBucket)/index/\(parameter.index)"
	// The query is embedded in the pod's own region (the cluster's), independent of
	// where the vector bucket lives.
	let _modelArn = "arn:aws:bedrock:{{ .Values.global.awsRegion }}::foundation-model/\(parameter.embeddingModel)"


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
			// Omitted unless set, so an hpa trait can own replicas (see mcp-server.cue).
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
						ports: [{
							name:          "mcp"
							containerPort: _port
							protocol:      "TCP"
						}]
						env: list.Concat([[
							{name: "S3VECTORS_BUCKET", value: parameter.vectorBucket},
							{name: "S3VECTORS_INDEX", value: parameter.index},
							{name: "S3VECTORS_REGION", value: parameter.region},
							{name: "EMBEDDING_MODEL", value: parameter.embeddingModel},
							{name: "TEXT_METADATA_KEY", value: parameter.textMetadataKey},
							{name: "DEFAULT_TOP_K", value: "\(parameter.defaultTopK)"},
							{name: "MAX_TOP_K", value: "\(parameter.maxTopK)"},
							{name: "COMPONENT_NAME", value: context.name},
							// Accept the pod network so the ClusterIP Service can reach it. Nothing
							// publishes the port beyond the cluster; the gateway is the entry point.
							{name: "HOST", value: "0.0.0.0"},
							{name: "PORT", value: "\(_port)"},
							if parameter.description != _|_ {
								{name: "INDEX_DESCRIPTION", value: parameter.description}
							},
						], parameter.env])
						// Liveness never depends on AWS; readiness waits for the index
						// to be described and the model checked against its dimension.
						livenessProbe: {
							httpGet: {
								path: "/healthz"
								port: _port
							}
							initialDelaySeconds: 5
							periodSeconds:       30
						}
						readinessProbe: {
							httpGet: {
								path: "/readyz"
								port: _port
							}
							initialDelaySeconds: 2
							periodSeconds:       5
						}
						resources: parameter.resources
						securityContext: {
							allowPrivilegeEscalation: false
							runAsNonRoot:             true
							capabilities: drop: ["ALL"]
						}
					}]
				}
			}
		}
	}

	// ServiceAccount token subjects for allowedAgents ("<ns>/<name>" or a bare name in
	// this namespace), as in mcp-server.cue.
	_agentSubjects: [
		for a in parameter.allowedAgents {
			if strings.Contains(a, "/") {
				"\"system:serviceaccount:" + strings.Replace(a, "/", ":", 1) + "\""
			}
			if !strings.Contains(a, "/") {
				"\"system:serviceaccount:" + context.namespace + ":" + a + "\""
			}
		},
	]

	outputs: {
		serviceAccount: {
			apiVersion: "v1"
			kind:       "ServiceAccount"
			metadata: {
				name:      context.name
				namespace: context.namespace
				labels: "app.kubernetes.io/name": context.name
			}
		}

		stableService: {
			apiVersion: "v1"
			kind:       "Service"
			metadata: {
				name:      context.name + "-stable"
				namespace: context.namespace
				labels: {
					"app.kubernetes.io/name":  context.name
					"agentgateway.dev/target": context.name
				}
			}
			spec: {
				selector: "app.kubernetes.io/name": context.name
				ports: [{
					name:        "mcp"
					port:        80
					targetPort:  _port
					protocol:    "TCP"
					appProtocol: "agentgateway.dev/mcp"
				}]
				type: "ClusterIP"
			}
		}

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
					port:        80
					targetPort:  _port
					protocol:    "TCP"
					appProtocol: "agentgateway.dev/mcp"
				}]
				type: "ClusterIP"
			}
		}

		// The server runs MCP stateless (no per-session state), so requests need not
		// follow a session to one pod.
		mcpBackend: {
			apiVersion: "agentgateway.dev/v1alpha1"
			kind:       "AgentgatewayBackend"
			metadata: {
				name:      context.name + "-backend"
				namespace: context.namespace
				labels: "app.kubernetes.io/name": context.name
			}
			spec: mcp: {
				targets: [{
					name: context.name + "-target"
					selector: services: matchLabels: "agentgateway.dev/target": context.name
				}]
				sessionRouting: "Stateless"
			}
		}

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

		// Only these agents may call this server (Require; see mcp-server.cue for why
		// Allow would restrict nothing).
		if parameter.registerWithGateway && len(_agentSubjects) > 0 {
			agentAccessPolicy: {
				apiVersion: "agentgateway.dev/v1alpha1"
				kind:       "AgentgatewayPolicy"
				metadata: {
					name:      context.name + "-allowed-agents"
					namespace: context.namespace
					labels: "app.kubernetes.io/name": context.name
				}
				spec: {
					targetRefs: [{
						group: "gateway.networking.k8s.io"
						kind:  "HTTPRoute"
						name:  context.name
					}]
					traffic: authorization: {
						action: "Require"
						policy: matchExpressions: ["jwt.sub in [" + strings.Join(_agentSubjects, ", ") + "]"]
					}
				}
			}
		}

		if len(parameter.authPolicy.matchExpressions) > 0 {
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

		// The server trusts nothing but the gateway: the gateway authenticates callers
		// and applies allowedAgents / mcpAccess, so traffic that reaches the pod any
		// other way bypasses both. Admit only the gateway's namespace.
		//
		// Enforced only where the cluster runs a network policy controller. On EKS
		// Auto Mode that needs the kube-system/amazon-vpc-cni ConfigMap with
		// enable-network-policy-controller: "true"; without it this object is
		// accepted and has no effect.
		if parameter.networkPolicy {
			ingressPolicy: {
				apiVersion: "networking.k8s.io/v1"
				kind:       "NetworkPolicy"
				metadata: {
					name:      context.name + "-gateway-only"
					namespace: context.namespace
					labels: "app.kubernetes.io/name": context.name
				}
				spec: {
					podSelector: matchLabels: "app.kubernetes.io/name": context.name
					policyTypes: ["Ingress"]
					ingress: [{
						from: [{namespaceSelector: matchLabels: "kubernetes.io/metadata.name": parameter.gatewayNamespace}]
						ports: [{protocol: "TCP", port: _port}]
					}]
				}
			}
		}

		// Read-only access to THIS index and query embedding with THIS model, nothing
		// else. QueryVectors with returnMetadata or a filter also requires GetVectors
		// (QueryVectors API reference); GetIndex lets the server read the dimension.
		// The name is the aws-service-identity accessFor convention; IAM names are
		// account-wide, see issue #92.
		"\(context.name)-iam-policy": {
			apiVersion: "iam.aws.upbound.io/v1beta1"
			kind:       "Policy"
			metadata: name: "\(context.appName)-\(context.name)-iam-policy"
			spec: {
				forProvider: {
					name: "\(context.appName)-\(context.name)-iam-policy"
					policy: json.Marshal({Version: "2012-10-17", Statement: _statements})
				}
				providerConfigRef: name: "default"
			}
		}
	}

	let _statements = [
		{
			Sid:    "QueryIndex"
			Effect: "Allow"
			Action: ["s3vectors:QueryVectors", "s3vectors:GetVectors", "s3vectors:GetIndex"]
			Resource: _indexArn
		},
		{
			Sid:      "EmbedQuery"
			Effect:   "Allow"
			Action:   ["bedrock:InvokeModel"]
			Resource: _modelArn
		},
		if parameter.kmsKeyArn != _|_ {
			{
				Sid:      "DecryptVectors"
				Effect:   "Allow"
				Action:   ["kms:Decrypt"]
				Resource: parameter.kmsKeyArn
				Condition: StringEquals: "kms:ViaService": "s3vectors.\(parameter.region).amazonaws.com"
			}
		},
	]

	parameter: {
		// +usage=Name of the EXISTING vector bucket (created by the ingestion pipeline)
		vectorBucket: string & =~"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$"
		// +usage=Name of the EXISTING vector index in that bucket
		index: string & =~"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$"
		// +usage=Bedrock embedding model used to embed QUERIES. Must be the model the ingestion pipeline embedded the documents with; the server refuses readiness if its output size cannot match the index dimension. Supported: amazon.titan-embed-text-v2:0 (256/512/1024), amazon.titan-embed-text-v1 (1536), cohere.embed-english-v3 and cohere.embed-multilingual-v3 (1024)
		embeddingModel: *"amazon.titan-embed-text-v2:0" | string & =~"^(amazon|cohere)\\.[a-z0-9.:-]+$"
		// +usage=Metadata key under which the pipeline stores each chunk's text. Returned as the result's text; every other key is returned as metadata (e.g. source, page)
		textMetadataKey: *"text" | string
		// +usage=What the index contains, in plain words. Shown to agents in the tool description so they know when to search it
		description?: string
		// +usage=Region of the vector bucket. Defaults to the cluster's region so the Application stays portable; override only for a bucket in another region
		region: *"{{ .Values.global.awsRegion }}" | string
		// +usage=Customer managed KMS key ARN, only if the vector bucket uses SSE-KMS with one. Grants kms:Decrypt via S3 Vectors; the key policy must also allow this workload's role
		kmsKeyArn?: string & =~"^arn:aws[a-z-]*:kms:[a-z0-9-]+:[0-9]{12}:key/.+$"
		// +usage=Results returned when the agent does not ask for a number
		defaultTopK: *5 | int & >=1 & <=100
		// +usage=Upper bound on results per search
		maxTopK: *20 | int & >=1 & <=100
		// +usage=Server image
		image: *"public.ecr.aws/z0a4o2j5/s3-vectors-mcp:0.1.0" | string
		// +usage=Number of replicas. Omit to let an hpa trait own replicas. The server is stateless, so any count is safe
		replicas?: int
		// +usage=Extra environment variables
		env: *[] | [...{
			name:  string
			value: string
		}]
		// +usage=Resource requests/limits
		resources: *{
			requests: {cpu: "50m", memory: "128Mi"}
			limits: memory: "256Mi"
		} | {
			requests?: {
				cpu?:    string
				memory?: string
			}
			limits?: {
				cpu?:    string
				memory?: string
			}
		}
		// +usage=Blue-green auto-promotion
		autoPromotionEnabled:   *true | bool
		autoPromotionSeconds?:  int
		scaleDownDelaySeconds?: int
		// +usage=Register an HTTPRoute on the gateway at /mcp/<name>
		registerWithGateway: *true | bool
		// +usage=Namespace of the agentgateway-proxy Gateway
		gatewayNamespace: *"agentgateway-system" | string
		// +usage=Admit traffic to the pod only from the gateway's namespace (NetworkPolicy). Effective only where the cluster enforces NetworkPolicy
		networkPolicy: *true | bool
		// +usage=Agents allowed to call this server, e.g. ["my-agent", "other-ns/agent-b"]. Empty (default) allows every authenticated agent. The platform's mcpAccess can narrow it further
		allowedAgents: *[] | [...string]
		// +usage=Tool-level authorization policy (CEL). Concrete with defaults (see mcp-server.cue, issue #61)
		authPolicy: {
			action:           *"Allow" | "Deny" | "Require"
			matchExpressions: *[] | [...string]
		}
	}
}
