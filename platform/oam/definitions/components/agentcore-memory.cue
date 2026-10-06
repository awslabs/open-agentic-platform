import "strings"

"agentcore-memory": {
	alias: ""
	annotations: {}
	attributes: {
		workload: type: "autodetects.core.oam.dev"
		status: {
			healthPolicy: "isHealth: *( context.output.status.atProvider.id != \"\" ) | false"
			customStatus: #"""
				message: *("memoryId: " + context.output.status.atProvider.id) | "provisioning"
				memoryId: *context.output.status.atProvider.id | ""
				"""#
		}
	}
	description: "AgentCore Memory provisioned via Crossplane managed resource with IAM policy, with optional long-term strategies"
	labels: {}
	type: "component"
}

template: {
	let _autoName = strings.Replace(context.namespace + "_" + context.name, "-", "_", -1)

	output: {
		apiVersion: "bedrockagentcore.aws.upbound.io/v1beta1"
		kind:       "Memory"
		metadata: name: context.name
		spec: {
			forProvider: {
				name:                parameter.memoryName
				region:              parameter.region
				description:         parameter.description
				eventExpiryDuration: parameter.eventExpiryDuration
			}
			providerConfigRef: name: "default"
		}
	}

	// Long-term memory strategies, opt-in. Each emits a MemoryStrategy bound to the
	// Memory above by memoryIdRef, so no id is copied anywhere. The namespace
	// templates are platform-owned and keyed by {actorId}, the caller identity the
	// agent takes from the gateway's X-Forwarded-User header, so one caller's
	// memories are never retrieved for another. The agent discovers these
	// namespaces from the memory itself (GetMemory) and needs no extra config.
	let _strategyTypes = {
		semantic: {type: "SEMANTIC", namespaces: ["/facts/{actorId}/"]}
		userPreference: {type: "USER_PREFERENCE", namespaces: ["/preferences/{actorId}/"]}
		summary: {type: "SUMMARIZATION", namespaces: ["/summaries/{actorId}/{sessionId}/"]}
	}
	outputs: {
		for s in parameter.strategies {
			"\(context.name)-strategy-\(strings.ToLower(s))": {
				apiVersion: "bedrockagentcore.aws.upbound.io/v1beta1"
				kind:       "MemoryStrategy"
				metadata: name: "\(context.name)-\(strings.ToLower(s))"
				spec: {
					forProvider: {
						// Must match ^[a-zA-Z][a-zA-Z0-9_]{0,47}$ (CreateMemory API model).
						name:       s
						region:     parameter.region
						type:       _strategyTypes[s].type
						namespaces: _strategyTypes[s].namespaces
						memoryIdRef: name: context.name
					}
					providerConfigRef: name: "default"
				}
			}
		}
	}

	outputs: "\(context.name)-iam-policy": {
		apiVersion: "iam.aws.upbound.io/v1beta1"
		kind:       "Policy"
		metadata: name: "\(context.appName)-\(context.name)-iam-policy"
		spec: {
			forProvider: {
				name: "\(context.appName)-\(context.name)-iam-policy"
				policy: """
					{
					  "Version": "2012-10-17",
					  "Statement": [
					    {
					      "Effect": "Allow",
					      "Action": ["bedrock-agentcore:*"],
					      "Resource": "*"
					    },
					    {
					      "Effect": "Allow",
					      "Action": [
					        "bedrock:InvokeModel",
					        "bedrock:InvokeModelWithResponseStream"
					      ],
					      "Resource": "*"
					    }
					  ]
					}
					"""
			}
			providerConfigRef: name: "default"
		}
	}

	parameter: {
		// +usage=Memory name in AWS (must match ^[a-zA-Z][a-zA-Z0-9_]{0,47}$). Defaults to <namespace>_<componentName>
		memoryName: *_autoName | string
		// +usage=AWS region. Defaults to the cluster's region so the same OAM Application
		// is portable across regions; override only for a cross-region memory store.
		region: *"{{ .Values.global.awsRegion }}" | string
		// +usage=Description of the memory
		description: *"AgentCore Memory" | string
		// +usage=Number of days after which events expire (3-365)
		eventExpiryDuration: *30 | int
		// +usage=Long-term memory strategies to enable. Empty (default) keeps short-term memory only: the conversation within a session. semantic stores facts, userPreference stores preferences, summary stores per-session summaries; all are kept per caller.
		strategies: *[] | [...("semantic" | "userPreference" | "summary")]
	}
}
