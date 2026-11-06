{{/*
AWS (and Crossplane object) name of the decentralized-observability IAM role.

IAM role names are unique per ACCOUNT, but this chart renders one Role per CLUSTER. A fixed
name made every cluster (and every install) in one account adopt the same role, so deleting
one cluster's object could delete the role the others still use, and the role carried no
install tag, so a prefix-based teardown left it behind.

  1. decentralizedObservability.roleName set  -> that name, unchanged (explicit override).
  2. global.clusterName set                    -> "<cluster>-agent-cw-observability-role".
     <cluster> is the cluster name lowercased with "_" mapped to "-", the same rule as the
     install-scoped component names (platform/oam/definitions/components/*.cue): the role
     name is also the Crossplane object name, which must be a DNS-1123 subdomain, and EKS
     allows uppercase and "_" in cluster names. EKS cluster names are unique per account
     and region. When the name is longer than the 64-character IAM limit:
     "<first 43 chars of cluster>-<8 hex>-cw-obs-role", the hex being
     sha256("<region>/<cluster as given>"), so two long names never truncate to one role.
  3. neither                                   -> "" (no name). global.clusterName is
     required: without it the Role and its default/default association are not rendered,
     and the decentralized-observability-identity trait fails the render (FAIL CLOSED in
     cw-observability-role.yaml and in the trait's CUE source), instead of falling back to a
     fixed name shared by every install of the account (#92).

Used by cw-observability-role.yaml (the Role and the default/default association) and by the
decentralized-observability-identity trait (platform/oam/definitions/traits/*.cue), which has no
roleName parameter: an Application cannot point its association at another role.
*/}}
{{- define "oam-agent-components.cwObservabilityRoleName" -}}
{{- $dobs := .Values.decentralizedObservability -}}
{{- $rawCluster := default "" .Values.global.clusterName -}}
{{- $cluster := lower (replace "_" "-" $rawCluster) -}}
{{- if $dobs.roleName -}}
{{- $dobs.roleName -}}
{{- else if $cluster -}}
{{- $name := printf "%s-agent-cw-observability-role" $cluster -}}
{{- if le (len $name) 64 -}}
{{- $name -}}
{{- else -}}
{{- $hash := trunc 8 (sha256sum (printf "%s/%s" (default "" .Values.global.awsRegion) $rawCluster)) -}}
{{- printf "%s-%s-cw-obs-role" (trimSuffix "-" (trunc 43 $cluster)) $hash -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
Name of the default/default PodIdentityAssociation OBJECT (a Kubernetes object name only, no
AWS resource carries it). Kept at its pre-existing name (the explicit roleName, else the
pre-existing literal) so a change of role name updates the
existing association in place (UpdatePodIdentityAssociation) instead of creating a second
association for the same namespace/ServiceAccount, which EKS rejects until the old one is
pruned.
*/}}
{{- define "oam-agent-components.cwObservabilityAssociationName" -}}
{{- default "agent-cw-observability-role" .Values.decentralizedObservability.roleName -}}
{{- end -}}

{{/*
Ownership tags for AWS resources this chart creates. platform.gitops.io/prefix is the tag a
prefix-scoped teardown uses to find out-of-CloudFormation resources it owns; it is set only
when global.resourcePrefix is (from the cluster secret's resource_prefix annotation).
*/}}
{{- define "oam-agent-components.awsTags" -}}
{{- with .Values.global.resourcePrefix }}
platform.gitops.io/prefix: {{ . | quote }}
{{- end }}
{{- with .Values.global.clusterName }}
platform.gitops.io/cluster: {{ . | quote }}
{{- end }}
platform.gitops.io/component: "oam-agent-components"
{{- end -}}
