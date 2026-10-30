import (
	"crypto/sha256"
	"encoding/hex"
	"strings"
)

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
	// AWS name of the IAM policy. IAM policy names are unique per ACCOUNT, not per region or
	// cluster, so two installs in one account that derive <appName>-<component>-iam-policy
	// ask for the same policy. With an install identity (global.clusterName, the cluster
	// secret's aws_cluster_name annotation, which the platform already passes):
	// <cluster>.<region>.<appName>-<component>-iam-policy. Cluster names (alphanumerics, "-"
	// and "_") and regions contain no ".", so the first two "."-separated fields identify the
	// install without ambiguity; the region is there because one account can hold the same
	// cluster name in two regions. It starts with the cluster name, hence with the resource
	// prefix. Past IAM's 128 characters: its first 100 characters, "-", 16 hex characters of
	// sha256(cluster/region/appName/component), "-iam-policy" (128). Without an install
	// identity: <appName>-<component>-iam-policy, as before.
	// Only the AWS name changes. The Policy OBJECT keeps <appName>-<component>-iam-policy,
	// which aws-service-identity's accessFor looks up (policyArnRef), so nothing that
	// references the policy moves.
	let _install = "{{ .Values.global.clusterName }}"
	let _installRegion = "{{ .Values.global.awsRegion }}"
	let _policyLegacy = context.appName + "-" + context.name + "-iam-policy"
	let _policyPlain = _install + "." + _installRegion + "." + _policyLegacy
	let _policyHash = strings.SliceRunes(hex.Encode(sha256.Sum256(_install + "/" + _installRegion + "/" + context.appName + "/" + context.name)), 0, 16)
	let _policyName = [if _install == "" {_policyLegacy}, if len(_policyPlain) <= 128 {_policyPlain}, strings.SliceRunes(_policyPlain, 0, 100) + "-" + _policyHash + "-iam-policy"][0]

	// Emit ONLY a Crossplane IAM Policy named "<appName>-<component>-iam-policy" so the
	// aws-service-identity trait (accessFor: [<this component name>]) attaches it to the
	// agent role. No AWS workload is created — the policy IS the component's output.
	output: {
		apiVersion: "iam.aws.upbound.io/v1beta1"
		kind:       "Policy"
		metadata: name: "\(context.appName)-\(context.name)-iam-policy"
		spec: {
			forProvider: {
				name: _policyName
				policy: """
					{"Version":"2012-10-17","Statement":[{"Sid":"EksReadDiscover","Effect":"Allow","Action":["eks:ListClusters","eks:DescribeCluster","eks:AccessKubernetesApi"],"Resource":"*"}]}
					"""
			}
			providerConfigRef: name: "default"
		}
	}
}
