{{/*
AWS (and Crossplane object) name of the decentralized-observability IAM role.

IAM role names are unique per ACCOUNT, but this chart renders one Role per CLUSTER. A fixed
name made every cluster (and every install) in one account adopt the same role, so deleting
one cluster's object could delete the role the others still use, and the role carried no
install tag, so a prefix-based teardown left it behind.

  1. decentralizedObservability.roleName set  -> that name, unchanged (explicit override).
  2. global.clusterName set                    -> "<cluster>-agent-cw-observability-role".
     EKS cluster names are unique per account and region. When that is longer than the
     64-character IAM limit: "<first 43 chars of cluster>-<8 hex>-cw-obs-role", the hex
     being sha256("<region>/<cluster>"), so two long names never truncate to one role.
  3. neither                                   -> legacy "agent-cw-observability-role".
*/}}
{{- define "oam-agent-components.cwObservabilityRoleName" -}}
{{- $dobs := .Values.decentralizedObservability -}}
{{- $cluster := lower (default "" .Values.global.clusterName) -}}
{{- if $dobs.roleName -}}
{{- $dobs.roleName -}}
{{- else if $cluster -}}
{{- $name := printf "%s-agent-cw-observability-role" $cluster -}}
{{- if le (len $name) 64 -}}
{{- $name -}}
{{- else -}}
{{- $hash := trunc 8 (sha256sum (printf "%s/%s" (default "" .Values.global.awsRegion) $cluster)) -}}
{{- printf "%s-%s-cw-obs-role" (trimSuffix "-" (trunc 43 $cluster)) $hash -}}
{{- end -}}
{{- else -}}
agent-cw-observability-role
{{- end -}}
{{- end -}}

{{/*
Name of the default/default PodIdentityAssociation OBJECT. Kept at its pre-existing name
(the explicit roleName, else the legacy literal) so a change of role name updates the
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
