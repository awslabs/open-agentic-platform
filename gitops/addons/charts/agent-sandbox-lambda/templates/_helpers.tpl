{{/*
Common labels applied to every resource this chart renders. Kept under the
app.kubernetes.io/name "agent-sandbox" (same capability, Lambda substrate) so
Flow D resources associate with the Agent Sandbox capability; the chart name
distinguishes them.
*/}}
{{- define "agent-sandbox.labels" -}}
app.kubernetes.io/name: agent-sandbox
app.kubernetes.io/component: lambda-microvm
app.kubernetes.io/part-of: open-agent-platform
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{/*
Selector labels (stable subset used by controllers).
*/}}
{{- define "agent-sandbox.selectorLabels" -}}
app.kubernetes.io/name: agent-sandbox
app.kubernetes.io/component: lambda-microvm
{{- end -}}

{{/*
The namespace the capability runs in (must match the Kata agent-sandbox chart).
*/}}
{{- define "agent-sandbox.namespace" -}}
{{- default "agent-sandbox-system" .Values.namespace -}}
{{- end -}}
