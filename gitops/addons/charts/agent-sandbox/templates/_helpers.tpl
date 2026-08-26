{{/*
Common labels applied to every resource this chart renders.
*/}}
{{- define "agent-sandbox.labels" -}}
app.kubernetes.io/name: agent-sandbox
app.kubernetes.io/part-of: open-agent-platform
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{/*
Selector labels (stable subset used by Services / controllers).
*/}}
{{- define "agent-sandbox.selectorLabels" -}}
app.kubernetes.io/name: agent-sandbox
{{- end -}}

{{/*
The namespace the capability runs in.
*/}}
{{- define "agent-sandbox.namespace" -}}
{{- default "agent-sandbox-system" .Values.namespace -}}
{{- end -}}
