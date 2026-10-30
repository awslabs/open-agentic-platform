{{/*
ESO template for the collector's Langfuse OTLP auth token.
The collector sends `Authorization: Basic ${LANGFUSE_OTEL_AUTH}`, so the Secret
VALUE must be base64("publicKey:secretKey"). ESO stores the rendered output as
the Secret value (the base64 in `.data` is only Kubernetes' storage encoding),
so b64enc here is required. Without it Langfuse returns HTTP 401.
*/}}
{{- define "otel-collector.langfuseAuthTemplate" -}}
{{`{{ printf "%s:%s" .publicKey .secretKey | b64enc }}`}}
{{- end -}}

{{/*
Content-addressed name for the Secret ESO writes. The hash covers the token
template and the SM key, so any change produces a NEW Secret name. The collector
references this exact name with optional: false, so kubelet only starts it once
ESO has rendered the current template. A pod can never start with a missing or
stale token (fresh env race, or an upgrade that changes the template).
*/}}
{{- define "otel-collector.langfuseAuthSecretName" -}}
{{- printf "langfuse-otel-auth-%s" (printf "%s|%s" (include "otel-collector.langfuseAuthTemplate" .) (.Values.langfuse.secretManagerKey | default "") | sha256sum | trunc 8) -}}
{{- end -}}

{{/*
AMP remote write is on when enabled and an endpoint source exists: the literal
amp.remoteWriteEndpoint, or the amp.secretManagerKey Secrets Manager secret
(read by ESO into the otel-collector-amp Secret). Returns "true" or "".
*/}}
{{- define "otel-collector.ampActive" -}}
{{- if and .Values.amp.enabled (or .Values.amp.remoteWriteEndpoint .Values.amp.secretManagerKey) -}}true{{- end -}}
{{- end -}}

{{/* True when the endpoint comes from Secrets Manager (no literal endpoint). */}}
{{- define "otel-collector.ampFromSecret" -}}
{{- if and (include "otel-collector.ampActive" .) (not .Values.amp.remoteWriteEndpoint) -}}true{{- end -}}
{{- end -}}

{{/* AMP with a literal endpoint (amp_endpoint_url annotation): exported from the main collector, as before. */}}
{{- define "otel-collector.ampLiteral" -}}
{{- if and (include "otel-collector.ampActive" .) .Values.amp.remoteWriteEndpoint -}}true{{- end -}}
{{- end -}}

{{/*
Endpoint from Secrets Manager: the bifrost metrics pipeline runs in its own
Deployment (otel-collector-metrics, amp-metrics.yaml). Only that Deployment waits
for the otel-collector-amp Secret, so a missing or late AMP secret never holds up
the traces collector.
*/}}
{{- define "otel-collector.metricsSplit" -}}
{{- if and (include "otel-collector.ampFromSecret" .) .Values.bifrost.enabled -}}true{{- end -}}
{{- end -}}

{{/* The main collector scrapes bifrost itself unless that moved to otel-collector-metrics. */}}
{{- define "otel-collector.metricsInMain" -}}
{{- if and .Values.bifrost.enabled (not (include "otel-collector.ampFromSecret" .)) -}}true{{- end -}}
{{- end -}}

{{/*
IAM Role + PodIdentityAssociation for AMP remote write. Always rendered on the
literal-endpoint path (unchanged). On the Secrets Manager path it can be turned off
with amp.podIdentity=false, for clusters where another chart already associates
otel/otel-collector (EKS allows one association per service account). Compared as a
string because ApplicationSet valuesObject templates render "false", not false.
*/}}
{{- define "otel-collector.podIdentity" -}}
{{- if and (include "otel-collector.ampActive" .) (or .Values.amp.remoteWriteEndpoint (ne (toString .Values.amp.podIdentity) "false")) -}}true{{- end -}}
{{- end -}}
