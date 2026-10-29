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
