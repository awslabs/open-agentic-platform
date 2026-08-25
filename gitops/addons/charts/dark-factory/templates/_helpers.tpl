{{- define "dark-factory.labels" -}}
app.kubernetes.io/name: dark-factory
app.kubernetes.io/part-of: dark-factory
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{/*
dark-factory.requiredChecks — the dark-factory/* commit statuses merge.js must see
GREEN (and PRESENT) on the PR head before it will merge. Comma-separated.

Derived from the enabled flags rather than hardcoded, because each gate is
individually switchable and merge.js now treats an ABSENT status as a refusal. A
fixed list would demand a status that a disabled gate never posts and block every
merge; ignoring the flags entirely would let a gate that IS enabled go unchecked.

  implementation  always — posted by the coder inside the VM, not gated
  holdout         holdout.enabled     (templates/20 … "dark-factory/holdout")
  security        review.enabled      (posted as "dark-factory/" + role)
  devops          review.enabled      (same step, role=devops)
  deploy-test     deployTest.enabled  (templates/20 … "dark-factory/deploy-test")
*/}}
{{- define "dark-factory.requiredChecks" -}}
{{- $checks := list "dark-factory/implementation" -}}
{{- if .Values.holdout.enabled -}}
{{- $checks = append $checks "dark-factory/holdout" -}}
{{- end -}}
{{- if .Values.review.enabled -}}
{{- $checks = append $checks "dark-factory/security" -}}
{{- $checks = append $checks "dark-factory/devops" -}}
{{- end -}}
{{- if .Values.deployTest.enabled -}}
{{- $checks = append $checks "dark-factory/deploy-test" -}}
{{- end -}}
{{- join "," $checks -}}
{{- end -}}
