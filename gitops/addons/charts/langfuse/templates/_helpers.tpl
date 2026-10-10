{{/*
Resource prefix for this deployment (e.g. "peeks"), sourced from the
cluster-secret resource_prefix annotation via the registry entry's
valuesObject.global.resourcePrefix.

Fail the render if it is unset/empty rather than silently producing an
identifier like "-hub/langfuse-otel": that bogus path would make the OTEL
Secrets Manager seed and every ExternalSecret that reads it resolve the wrong
(or a cross-event-colliding) secret. Helm's `required` treats an empty string
as missing, so an absent annotation is caught here.
*/}}
{{- define "langfuse.resourcePrefix" -}}
{{- required "langfuse: global.resourcePrefix must be set (from the cluster-secret resource_prefix annotation); refusing to render a secret path like \"-hub/langfuse-otel\"" .Values.global.resourcePrefix -}}
{{- end -}}
