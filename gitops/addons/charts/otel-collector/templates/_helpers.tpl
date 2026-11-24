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
{{/*
Guardrail access-log metrics (docs/architecture/agent-guardrails.md, D8). The gateway exports
one OTLP log record per /llm request; a count connector turns those records into counters
and the AMP exporter writes them. True when guardrails.accessLog.enabled is "true" (a
string, because the registry renders text).
*/}}
{{- define "otel-collector.guardrailLogs" -}}
{{- if eq (toString .Values.guardrails.accessLog.enabled) "true" -}}true{{- end -}}
{{- end -}}
{{/*
The pipeline lives in whichever collector writes to AMP, so one stream of counters has one
writer. With the AMP workspace from Secrets Manager that is otel-collector-metrics
(amp-metrics.yaml); otherwise it is the main collector.
*/}}
{{- define "otel-collector.guardrailLogsMetrics" -}}
{{- if and (include "otel-collector.guardrailLogs" .) (include "otel-collector.metricsSplit" .) -}}true{{- end -}}
{{- end -}}
{{- define "otel-collector.guardrailLogsMain" -}}
{{- if and (include "otel-collector.guardrailLogs" .) (not (include "otel-collector.metricsSplit" .)) -}}true{{- end -}}
{{- end -}}
{{/* Where the counters go: AMP when this collector has the AMP exporter, else the debug exporter. */}}
{{- define "otel-collector.guardrailLogsExporter" -}}
{{- if or (include "otel-collector.ampLiteral" .) (include "otel-collector.guardrailLogsMetrics" .) -}}prometheusremotewrite/amp{{- else -}}debug/guardrail{{- end -}}
{{- end -}}
{{/* Image of a collector that hosts the pipeline: the count connector is not in the ADOT build. */}}
{{- define "otel-collector.guardrailLogsImage" -}}
{{- .Values.guardrails.accessLog.image -}}
{{- end -}}
{{/*
Connector, processor and pipelines for the access-log counters, as config.yaml sections.
The record carries only these attributes (allowlist set in agent-gateway
templates/guardrail-accesslog.yaml): agent, route, status, reason, guardrail,
guardrail_version. No prompt text, completion or guard response body is ever in a record, so
none can reach a label.
Two counters:
  oap.guardrail.accesslog.requests  one per request, by agent, route, status, reason, guardrail.
  oap.guardrail.accesslog.versions  by route and guardrail_version, no agent, so a Bedrock
                                    guardrail version never multiplies the per-agent series.
The count connector emits deltas and prometheusremotewrite only accepts cumulative sums, so
deltatocumulative sits between them. A stream with no record for max_stale is dropped, which
is what makes an idle agent absent instead of a flat line.
*/}}
{{- define "otel-collector.guardrailLogsConnectors" -}}
connectors:
  count/guardrail:
    logs:
      oap.guardrail.accesslog.requests:
        description: Gateway requests on the guardrail routes, by caller, route, status, error reason and guard action.
        attributes:
          - key: agent
          - key: route
          - key: status
          - key: reason
            default_value: none
          - key: guardrail
            default_value: none
      oap.guardrail.accesslog.versions:
        description: Requests by route and the guardrail version that checked them.
        attributes:
          - key: route
          - key: guardrail_version
{{- end -}}
{{- define "otel-collector.guardrailLogsProcessors" -}}
deltatocumulative/guardrail:
  max_stale: {{ .Values.guardrails.accessLog.maxStale }}
{{- end -}}
{{- define "otel-collector.guardrailLogsPipelines" -}}
logs/guardrail:
  receivers: [otlp]
  processors: [memory_limiter, batch]
  exporters: [count/guardrail]
metrics/guardrail:
  receivers: [count/guardrail]
  processors: [deltatocumulative/guardrail, batch]
  exporters: [{{ include "otel-collector.guardrailLogsExporter" . }}]
{{- end -}}
{{/*
ClusterIP Service the gateway exports its access log to, OTLP over HTTP. Cluster-internal
only. It selects the collector that hosts the pipeline.
*/}}
{{- define "otel-collector.guardrailLogsService" -}}
apiVersion: v1
kind: Service
metadata:
  name: {{ .ctx.Values.guardrails.accessLog.serviceName }}
  namespace: {{ .ctx.Release.Namespace }}
spec:
  selector:
    app: {{ .app }}
  ports:
    - name: otlp-http
      port: 4318
      targetPort: 4318
      protocol: TCP
  type: ClusterIP
{{- end -}}
