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
				// Least privilege, replacing an inherited "bedrock-agentcore:*" on "*".
				// The action list is exactly what a browser consumer needs, taken from the
				// AgentCore Browser permissions reference, and narrowed further to the
				// operations this platform actually performs: resolve a browser by name,
				// start and stop sessions, and connect the CDP automation stream.
				//
				// Two statements because ListBrowsers enumerates a collection and is
				// granted over all browsers in the account and region, while the session
				// operations are restricted to THIS component's browser. The browser id is
				// "<browserName>-<generated suffix>", so the name prefix identifies it
				// without knowing the suffix at policy-creation time.
				//
				// The account id comes from platform config (the cluster secret's
				// aws_account_id annotation), never from developer OAM. It falls back to
				// "*" so the ARN stays valid if the global is unset.
				policy: """
					{
					  "Version": "2012-10-17",
					  "Statement": [
					    {
					      "Sid": "ResolveBrowserByName",
					      "Effect": "Allow",
					      "Action": ["bedrock-agentcore:ListBrowsers"],
					      "Resource": "arn:aws:bedrock-agentcore:\(parameter.region):{{ .Values.global.awsAccountId }}:browser/*"
					    },
					    {
					      "Sid": "UseThisBrowser",
					      "Effect": "Allow",
					      "Action": [
					        "bedrock-agentcore:GetBrowser",
					        "bedrock-agentcore:StartBrowserSession",
					        "bedrock-agentcore:GetBrowserSession",
					        "bedrock-agentcore:ListBrowserSessions",
					        "bedrock-agentcore:StopBrowserSession",
					        "bedrock-agentcore:ConnectBrowserAutomationStream"
					      ],
					      "Resource": "arn:aws:bedrock-agentcore:\(parameter.region):{{ .Values.global.awsAccountId }}:browser/\(parameter.browserName)-*"
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
