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

{{/*
Checksum of everything that feeds langfuse-config and langfuse-secrets.

Used as a pod annotation on the web and worker Deployments so that a config
change actually rolls them. Kubernetes does NOT restart a pod when a ConfigMap
it consumes with envFrom changes, so without this a new key sits in the
ConfigMap while every running container keeps the values it booted with.
Observed exactly that: LANGFUSE_LLM_CONNECTION_WHITELISTED_HOST was present in
langfuse-config while a 93-minute-old pod went on refusing the evaluation
judge's LLM connection.

Deliberately scoped to the values those two objects are built from rather than
all of .Values: a change that only affects the Deployment (image tag, resources)
already triggers its own rollout, and hashing everything would roll the pods a
second time for no reason.

Keep this list in step with the ConfigMap and Secret in langfuse.yaml. A value
missing here is not a render error — it is a silent staleness bug of exactly the
kind this helper exists to prevent.
*/}}
{{- define "langfuse.configChecksum" -}}
{{- $inputs := dict
  "ingressHost"    (.Values.ingress).host
  "keycloak"       .Values.keycloak
  "minio"          .Values.minio
  "postgres"       .Values.postgres
  "clickhouse"     (.Values.clickhouse).password
  "awsRegion"      (.Values.global).awsRegion
  "nextauthSecret" .Values.nextauthSecret
  "salt"           .Values.salt
  "evaluation"     .Values.evaluation
  "migrationV4"    .Values.migrationV4
-}}
{{- toJson $inputs | sha256sum -}}
{{- end -}}
