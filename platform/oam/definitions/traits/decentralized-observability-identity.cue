// decentralized-observability-identity TraitDefinition
//
// Grants a workload CloudWatch Logs / X-Ray / CW Metrics write access for decentralized
// observability: a per-agent EKS PodIdentityAssociation binds the workload's own
// ServiceAccount (owned by the component, name == context.name) to the cluster's CW
// observability IAM role.
//
// The role itself is not created here. The chart renders one Role per cluster in
// templates/cw-observability-role.yaml, named by the oam-agent-components.cwObservabilityRoleName
// helper (templates/_helpers.tpl): "<global.clusterName>-agent-cw-observability-role", hashed
// when over the 64-character IAM limit, or the platform's decentralizedObservability.roleName.
// The trait takes the same name from that helper, so both sides always agree.
"decentralized-observability-identity": {
	alias:       ""
	annotations: {}
	attributes: {
		appliesToWorkloads: ["deployments.apps", "rollouts.argoproj.io"]
		conflictsWith: []
		podDisruptive:   false
	}
	description: "Grant a workload CloudWatch/X-Ray write access for decentralized observability via EKS Pod Identity"
	labels: {}
	type: "trait"
}

template: {
	// FAIL CLOSED without the install identity. The role is named after the cluster, and
	// without global.clusterName the chart renders no role: a fixed name would be shared by
	// every install of the account (#92). Fail the render instead of emitting an association
	// to a role that does not exist. The two strings conflict on purpose: KubeVela prints
	// both, so the error names the missing value.
	let _install = "{{ .Values.global.clusterName }}"
	if _install == "" {
		_installIdentity: "set" & "global.clusterName is empty: set it on the oam-agent-components chart (the cluster secret's aws_cluster_name annotation)"
	}

	// The cluster's role, as rendered by the chart. Not a parameter: Crossplane Role objects
	// are cluster-scoped and shared by every team, so a developer-chosen name could point the
	// association at another team's role and be granted its AWS permissions. The same rule
	// as memoryName in agentcore-memory. The template name uses a Go raw string (backquotes)
	// because Helm renders this file before CUE and nested double quotes break that parse.
	_roleName: "{{ include `oam-agent-components.cwObservabilityRoleName` . }}"

	// roleName was a parameter before. An Application that still sets it fails the render
	// with this message rather than being silently ignored. Same conflicting-strings trick.
	if parameter.roleName != _|_ {
		_roleNameRemoved: "unset" & "roleName is no longer accepted by decentralized-observability-identity: remove it from the trait properties. The trait always uses the cluster's CW observability role (<global.clusterName>-agent-cw-observability-role)"
	}

	parameter: {
		// +usage=AWS region for the PodIdentityAssociation
		region: *"{{ .Values.global.awsRegion }}" | string
		// +usage=EKS cluster name
		clusterName: *"{{ .Values.global.clusterName }}" | string
	}

	outputs: {
		"\(context.name)-cw-pod-identity": {
			apiVersion: "eks.aws.upbound.io/v1beta1"
			kind:       "PodIdentityAssociation"
			metadata: {
				name: "\(context.name)-cw-observability"
				annotations: {
					"argocd.argoproj.io/sync-options": "SkipDryRunOnMissingResource=true"
				}
			}
			spec: forProvider: {
				clusterName: parameter.clusterName
				namespace:   context.namespace
				region:      parameter.region
				// resolve: Always so an existing association follows a renamed role instead
				// of keeping the ARN it resolved first (see templates/cw-observability-role.yaml).
				roleArnRef: {
					name: _roleName
					policy: resolve: "Always"
				}
				serviceAccount: context.name
			}
		}
	}
}
