"eks-read-access": {
	alias: ""
	annotations: {}
	attributes: {
		workload: type: "autodetects.core.oam.dev"
		status: healthPolicy: "isHealth: *( context.output.status.atProvider.arn != \"\" ) | false"
	}
	description: "Read-only EKS discovery IAM policy (eks:ListClusters/DescribeCluster/AccessKubernetesApi) attachable to an agent's Pod Identity role via the aws-service-identity trait accessFor. Lets an agent that runs a read-only EKS/eks-read MCP discover and describe fleet clusters (get-token needs eks:DescribeCluster; cluster discovery needs eks:ListClusters). Cluster/object reads remain gated by per-cluster EKS AccessEntries."
	labels: {}
	type: "component"
}

template: {
	// Emit ONLY a Crossplane IAM Policy named "<appName>-<component>-iam-policy" so the
	// aws-service-identity trait (accessFor: [<this component name>]) attaches it to the
	// agent role. No AWS workload is created — the policy IS the component's output.
	output: {
		apiVersion: "iam.aws.upbound.io/v1beta1"
		kind:       "Policy"
		metadata: name: "\(context.appName)-\(context.name)-iam-policy"
		spec: {
			forProvider: {
				name: "\(context.appName)-\(context.name)-iam-policy"
				policy: """
					{"Version":"2012-10-17","Statement":[{"Sid":"EksReadDiscover","Effect":"Allow","Action":["eks:ListClusters","eks:DescribeCluster","eks:AccessKubernetesApi"],"Resource":"*"}]}
					"""
			}
			providerConfigRef: name: "default"
		}
	}
}
