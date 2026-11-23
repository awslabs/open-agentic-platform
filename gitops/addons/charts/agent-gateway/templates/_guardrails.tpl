{{/*
  Checks the guardrail catalog shared by the /llm templates: every profile is in the order
  and every name in the order has a profile; each profile sets action, failureMode and
  scope explicitly; the default profile exists; guardrailAccess names only profiles weaker
  than the default, with full <namespace>/<agent> entries. Fails the render otherwise.
  Call it once at the top of each template that renders per-profile objects.
*/}}
{{- define "agent-gateway.guardrailsValidate" -}}
{{- $g := .Values.guardrails -}}
{{- range $name, $_ := $g.profiles -}}
{{-   if not (has $name $g.order) -}}
{{-     fail (printf "agent-gateway: guardrails.profiles.%s is not listed in guardrails.order" $name) -}}
{{-   end -}}
{{- end -}}
{{- range $name := $g.order -}}
{{-   $p := get $g.profiles $name -}}
{{-   if not $p -}}
{{-     fail (printf "agent-gateway: guardrails.order lists %q but guardrails.profiles has no such profile" $name) -}}
{{-   end -}}
{{-   if not (has $p.action (list "Reject" "Audit")) -}}
{{-     fail (printf "agent-gateway: guardrails.profiles.%s.action must be Reject or Audit, got %q" $name (toString $p.action)) -}}
{{-   end -}}
{{-   if not (has $p.failureMode (list "FailClosed" "FailOpen")) -}}
{{-     fail (printf "agent-gateway: guardrails.profiles.%s.failureMode must be set explicitly to FailClosed or FailOpen, got %q" $name (toString $p.failureMode)) -}}
{{-   end -}}
{{-   if not $p.scope -}}
{{-     fail (printf "agent-gateway: guardrails.profiles.%s.scope must be set explicitly (a list of Messages, SystemPrompt, ToolInput, ToolOutput)" $name) -}}
{{-   end -}}
{{-   range $p.scope -}}
{{-     if not (has . (list "Messages" "SystemPrompt" "ToolInput" "ToolOutput")) -}}
{{-       fail (printf "agent-gateway: guardrails.profiles.%s.scope has unknown value %q" $name .) -}}
{{-     end -}}
{{-   end -}}
{{- end -}}
{{- if not (has $g.defaultProfile $g.order) -}}
{{-   fail (printf "agent-gateway: guardrails.defaultProfile %q is not in guardrails.order" $g.defaultProfile) -}}
{{- end -}}
{{- $defaultIdx := index (include "agent-gateway.guardrailIndexes" . | fromJson) $g.defaultProfile -}}
{{- $indexes := include "agent-gateway.guardrailIndexes" . | fromJson -}}
{{- range $name, $grant := .Values.guardrailAccess -}}
{{-   if not (hasKey $indexes $name) -}}
{{-     fail (printf "agent-gateway: guardrailAccess.%s names a profile that is not in guardrails.order" $name) -}}
{{-   end -}}
{{-   if ge (int (index $indexes $name)) (int $defaultIdx) -}}
{{-     fail (printf "agent-gateway: guardrailAccess.%s is set, but %s is at or above the default profile %s and already admits every agent" $name $name $g.defaultProfile) -}}
{{-   end -}}
{{-   range ($grant.agents | default list) -}}
{{-     if not (contains "/" .) -}}
{{-       fail (printf "agent-gateway: guardrailAccess.%s.agents entry %q must be <namespace>/<agent>" $name .) -}}
{{-     end -}}
{{-   end -}}
{{- end -}}
{{- end -}}
{{/* Profile name -> position in guardrails.order, as JSON. */}}
{{- define "agent-gateway.guardrailIndexes" -}}
{{- $out := dict -}}
{{- range $i, $name := .Values.guardrails.order -}}
{{- $_ := set $out $name $i -}}
{{- end -}}
{{- toJson $out -}}
{{- end -}}
{{/* In-cluster hostnames the /llm routes match, as a JSON list. */}}
{{- define "agent-gateway.guardrailHostnames" -}}
{{- $p := .Values.guardrails.proxy -}}
{{- list (printf "%s.%s.svc.%s" $p.gatewayName $p.namespace $p.clusterDomain) | toJson -}}
{{- end -}}
{{/* Pod spec shared by the binder Job and CronJob (guardrail-binder.yaml). */}}
{{- define "agent-gateway.binderPod" }}
serviceAccountName: guardrail-binder
restartPolicy: OnFailure
containers:
  - name: binder
    image: {{ .image | quote }}
    command: ["python", "/scripts/bind-guardrails.py"]
    env:
      - name: GUARDRAIL_PROFILES
        value: {{ .profiles | toJson | quote }}
      - name: POLICY_NAMESPACE
        value: {{ .policyNamespace | quote }}
      - name: DEADLINE_SECONDS
        value: {{ .deadline | quote }}
      - name: POLL_SECONDS
        value: {{ .poll | quote }}
    volumeMounts:
      - {name: script, mountPath: /scripts, readOnly: true}
    securityContext:
      allowPrivilegeEscalation: false
      readOnlyRootFilesystem: true
      runAsNonRoot: true
      runAsUser: 65532
      capabilities: {drop: ["ALL"]}
      seccompProfile: {type: RuntimeDefault}
    resources:
      requests: {cpu: 10m, memory: 32Mi}
      limits: {memory: 128Mi}
volumes:
  - name: script
    configMap: {name: guardrail-binder}
{{- end }}
