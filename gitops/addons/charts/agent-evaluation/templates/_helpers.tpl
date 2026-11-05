{{- define "agent-evaluation.name" -}}
agent-evaluation
{{- end -}}

{{- define "agent-evaluation.labels" -}}
app.kubernetes.io/name: {{ include "agent-evaluation.name" . }}
app.kubernetes.io/component: agent-evaluation-reconciler
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: agent-platform
{{- end -}}

{{- define "agent-evaluation.selectorLabels" -}}
app: agent-evaluation-reconciler
{{- end -}}

{{/*
FAIL CLOSED on the two values that have no safe default.

A missing clusterName would make every rule name, trace tag and metric series
collide with another cluster's in the one shared Langfuse project — exactly the
cross-cluster damage the per-cluster ownership rule exists to prevent. A missing
Langfuse URL would leave the reconciler in CrashLoopBackOff with a config error,
which is a slower and less obvious way to learn the same thing. Neither can be
guessed, so fail the render rather than install something that misbehaves.
*/}}
{{- define "agent-evaluation.clusterName" -}}
{{- $name := .Values.global.clusterName | default "" -}}
{{- if not $name -}}
{{- fail "agent-evaluation: global.clusterName is required. It scopes this cluster's Langfuse rule names, trace tags and metrics; without it two clusters would write each other's objects in the shared Langfuse project. The registry entry supplies it from the cluster secret's aws_cluster_name annotation." -}}
{{- end -}}
{{- $name -}}
{{- end -}}

{{- define "agent-evaluation.langfuseUrl" -}}
{{- $url := .Values.langfuse.url | default "" -}}
{{- if not $url -}}
{{- fail "agent-evaluation: langfuse.url is required. On the hub use http://langfuse.langfuse.svc.cluster.local:3000; a spoke uses https://<hub domain>, resolved by the registry entry the same way the otel-collector resolves its export endpoint." -}}
{{- end -}}
{{- $url -}}
{{- end -}}

{{- define "agent-evaluation.role" -}}
{{- $role := .Values.role | default "spoke" -}}
{{- if not (has $role (list "hub" "spoke")) -}}
{{- fail (printf "agent-evaluation: role must be 'hub' or 'spoke', got %q" $role) -}}
{{- end -}}
{{- $role -}}
{{- end -}}

{{- define "agent-evaluation.isHub" -}}
{{- eq (include "agent-evaluation.role" .) "hub" -}}
{{- end -}}

{{/*
Content-addressed annotation so a catalog or coverage change restarts the
reconciler. Without it a prompt edit would sit in the ConfigMap unread until
something else happened to restart the pod, and the hub would keep publishing
the old prompt — which is the opposite of "a prompt change reaches every cluster".
*/}}
{{- define "agent-evaluation.catalogChecksum" -}}
{{- $root := . -}}
{{- $parts := list (toJson .Values.catalog) (toJson .Values.coverage) (toJson .Values.judge) -}}
{{- range $name, $spec := .Values.catalog -}}
{{- $parts = append $parts ($root.Files.Get (printf "files/evaluators/%s" $spec.prompt) | default "") -}}
{{- end -}}
{{- join "\n" $parts | sha256sum -}}
{{- end -}}
