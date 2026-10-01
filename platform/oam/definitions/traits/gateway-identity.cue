// gateway-identity TraitDefinition
//
// Gives a workload a secretless identity to AgentGateway/MCP: mounts a projected
// ServiceAccount token scoped to the `agentgateway` audience. The workload reads
// the token file and presents it as a Bearer token; AgentGateway validates it
// against the cluster's EKS OIDC issuer (see the agent-gateway workloadIdentity
// provider). Auto-rotated by the kubelet; no secret to manage.
//
// Rides on the pod's ServiceAccount (owned by the component, name == context.name),
// so the token's `sub` (system:serviceaccount:<ns>:<name>) is the workload identity.
"gateway-identity": {
	alias:       ""
	annotations: {}
	attributes: {
		appliesToWorkloads: ["deployments.apps", "rollouts.argoproj.io"]
		conflictsWith: []
		podDisruptive:   true
		workloadRefPath: ""
	}
	description: "Give a workload a secretless identity to AgentGateway via a projected ServiceAccount token"
	labels: {}
	type: "trait"
}

template: {
	parameter: {
		// +usage=Audience stamped into the projected token; must match the gateway's expected audience
		audience: *"agentgateway" | string
		// +usage=Container to mount the token into (defaults to the component name)
		containerName: *context.name | string
	}

	_mountDir:   "/var/run/secrets/agentgateway"
	_tokenMount: {
		name:      "agentgateway-token"
		mountPath: _mountDir
		readOnly:  true
	}

	patch: spec: template: spec: {
		// +patchKey=name
		volumes: [{
			name: "agentgateway-token"
			projected: sources: [{
				serviceAccountToken: {
					audience:          parameter.audience
					expirationSeconds: 3600
					path:              "token"
				}
			}]
		}]
		// +patchKey=name
		containers: [{
			name: parameter.containerName
			// +patchKey=name
			env: [{
				name:  "WORKLOAD_TOKEN_PATH"
				value: "\(_mountDir)/token"
			}]
			// +patchKey=name
			volumeMounts: [_tokenMount]
		}]
	}
}
