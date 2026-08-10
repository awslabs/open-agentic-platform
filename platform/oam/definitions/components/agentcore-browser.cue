import "strings"

"agentcore-browser": {
	alias: ""
	annotations: {}
	attributes: {
		workload: type: "autodetects.core.oam.dev"
		status: {
			healthPolicy: "isHealth: *( context.output.status.atProvider.browserId != \"\" ) | false"
			customStatus: #"""
				message: *("browserId: " + context.output.status.atProvider.browserId) | "provisioning"
				browserId: *context.output.status.atProvider.browserId | ""
				"""#
		}
	}
	description: "AgentCore Browser provisioned via Crossplane managed resource with IAM policy"
	labels: {}
	type: "component"
}

template: {
	let _autoName = strings.Replace(context.namespace + "_" + context.name, "-", "_", -1)

	output: {
		apiVersion: "bedrockagentcore.aws.upbound.io/v1beta1"
		kind:       "Browser"
		metadata: name: context.name
		spec: {
			forProvider: {
				name:        parameter.browserName
				region:      parameter.region
				description: parameter.description
				networkConfiguration: {
					networkMode: parameter.networkMode
				}
			}
			providerConfigRef: name: "default"
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
					    }
					  ]
					}
					"""
			}
			providerConfigRef: name: "default"
		}
	}

	parameter: {
		// +usage=Browser name in AWS (must match ^[a-zA-Z][a-zA-Z0-9_]*$). Defaults to <namespace>_<componentName>
		browserName: *_autoName | string
		// +usage=AWS region. Defaults to the cluster's region so the same OAM Application
		// is portable across regions; override only for a cross-region browser.
		region: *"{{ .Values.global.awsRegion }}" | string
		// +usage=Description of the browser
		description: *"AgentCore Browser" | string
		// +usage=Network mode: PUBLIC or VPC
		networkMode: *"PUBLIC" | "VPC"
	}
}
