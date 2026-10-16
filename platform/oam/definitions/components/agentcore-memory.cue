import "strings"

"agentcore-memory": {
	alias: ""
	annotations: {}
	attributes: {
		workload: type: "autodetects.core.oam.dev"
		status: {
			// Healthy once the Memory AND every MemoryStrategy exist, so an agent that
			// dependsOn this component starts with all long-term strategies in place.
			// A MemoryStrategy reports its id as status.atProvider.memoryStrategyId (not
			// `id`, unlike the Memory), set once AgentCore has created it.
			healthPolicy: #"""
				_pending: [for k, v in context.outputs if v.kind == "MemoryStrategy" && v.status.atProvider.memoryStrategyId == _|_ {k}]
				isHealth: *(context.output.status.atProvider.id != "" && len(_pending) == 0) | false
				"""#
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
	// Memory and MemoryStrategy here are the cluster-scoped Upbound kinds
	// (*.aws.upbound.io), so their object names carry the namespace to stay unique
	// across namespaces (oam-authoring.md section 3). The IAM Policy keeps
	// <appName>-<component>-iam-policy: aws-service-identity's accessFor looks it up by
	// exactly that name, so it changes only together with that trait (appmod). IAM
	// policy names are account-global, so the same appName and component in two
	// namespaces collide on that Policy until the convention changes upstream.
	//
	// Upgrading from a version that named the Memory <component>: the object name
	// changes, so KubeVela deletes the old Memory and creates a new one. The AWS memory
	// and its stored events are deleted with it and a new memoryId is issued.
	let _objName = context.namespace + "-" + context.name

	output: {
		apiVersion: "bedrockagentcore.aws.upbound.io/v1beta1"
		kind:       "Memory"
		metadata: name: _objName
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
	// agent takes from the gateway's X-Forwarded-User header, so through the gateway
	// one caller's memories are never retrieved for another. A workload that reaches
	// agent pods directly, bypassing the gateway, can set that header; isolating pods
	// from such traffic is the operator's NetworkPolicy. The agent discovers these
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
				metadata: name: "\(_objName)-\(strings.ToLower(s))"
				spec: {
					forProvider: {
						// Must match ^[a-zA-Z][a-zA-Z0-9_]{0,47}$ (CreateMemory API model).
						name:       s
						region:     parameter.region
						type:       _strategyTypes[s].type
						namespaces: _strategyTypes[s].namespaces
						memoryIdRef: name: _objName
					}
					providerConfigRef: name: "default"
				}
			}
		}
	}

	// Only what the agent's AgentCore session manager calls (bedrock_agentcore
	// MemoryClient: get_memory for strategies, create/list/get/delete_event,
	// retrieve_memory_records), on this memory only. AgentCore ids are
	// "<memoryName>-<suffix>" and the memoryName parameter is constrained to exclude
	// "-", so the "-*" suffix matches this memory and no other.
	//
	// No bedrock:InvokeModel: the strategies emitted here are AgentCore built-ins,
	// whose extraction AgentCore runs itself, and the agent's model calls go to the
	// LLM gateway, not to Bedrock under this role.
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
					      "Action": [
					        "bedrock-agentcore:GetMemory",
					        "bedrock-agentcore:CreateEvent",
					        "bedrock-agentcore:ListEvents",
					        "bedrock-agentcore:GetEvent",
					        "bedrock-agentcore:DeleteEvent",
					        "bedrock-agentcore:RetrieveMemoryRecords"
					      ],
					      "Resource": "arn:aws:bedrock-agentcore:\(parameter.region):{{ .Values.global.awsAccountId }}:memory/\(parameter.memoryName)-*"
					    }
					  ]
					}
					"""
			}
			providerConfigRef: name: "default"
		}
	}

	parameter: {
		// +usage=Memory name in AWS (must match ^[a-zA-Z][a-zA-Z0-9_]{0,47}$). Defaults to <namespace>_<componentName> with "-" replaced by "_"
		// The constraint applies to the default too, so the IAM "<memoryName>-*" scoping
		// holds by construction and a bad name fails at render, not at the AWS call.
		memoryName: (*_autoName | string) & =~"^[a-zA-Z][a-zA-Z0-9_]{0,47}$"
		// +usage=AWS region. Defaults to the cluster's region so the same OAM Application
		// is portable across regions; override only for a cross-region memory store.
		region: *"{{ .Values.global.awsRegion }}" | string
		// +usage=Description of the memory
		description: *"AgentCore Memory" | string
		// +usage=Number of days after which events expire (3-365)
		eventExpiryDuration: *30 | int & >=3 & <=365
		// +usage=Long-term memory strategies to enable. Empty (default) keeps short-term memory only: the conversation within a session. semantic stores facts, userPreference stores preferences, summary stores per-session summaries; all are kept per caller. Removing a strategy from this list deletes that strategy in AgentCore (its MemoryStrategy is garbage-collected), and the agent stops retrieving its namespace.
		strategies: *[] | [...("semantic" | "userPreference" | "summary")]
	}
}
