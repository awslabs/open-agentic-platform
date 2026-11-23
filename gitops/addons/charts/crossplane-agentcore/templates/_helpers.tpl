{{/*
  The package-runtime container of a provider DeploymentRuntimeConfig: resources and the
  GOMEMLIMIT/GOGC env vars from .Values.provider.resources. Shared by every provider
  runtime config in this chart. Takes the root context and returns the list item, so call
  it as: {{ include "crossplane-agentcore.runtimeContainer" . | nindent 12 }}
*/}}
{{- define "crossplane-agentcore.runtimeContainer" -}}
- name: package-runtime
  {{- /* Same mechanism as appmod-blueprints crossplane-base
         (gitops/addons/charts/crossplane-base/templates/provider-config.yaml); see the
         provider.resources comment in values.yaml for the measurements.
         hasKey, not `| default`, so an explicit "" for goMemLimit/goGC means
         "do not set the env var". */}}
  {{- $r := dict "cpuRequest" "100m" "memoryRequest" "320Mi" "memoryLimit" "512Mi" "goGC" "50" }}
  {{- range $k, $v := (.Values.provider.resources | default dict) }}{{ $_ := set $r $k $v }}{{ end }}
  {{- /* GOMEMLIMIT defaults to 90% of the memory limit (Go GC guide: 5-10% headroom
         for non-Go memory), so it cannot drift away from the limit it protects. */}}
  {{- if not (hasKey $r "goMemLimit") }}
  {{-   $lim := toString $r.memoryLimit }}
  {{-   if not (regexMatch "^[1-9][0-9]*(Mi|Gi)$" $lim) }}
  {{-     fail (printf "crossplane-agentcore: provider.resources.memoryLimit %q must be a whole number of Mi or Gi (or set goMemLimit explicitly)" $lim) }}
  {{-   end }}
  {{-   $mib := atoi (trimSuffix "Mi" (trimSuffix "Gi" $lim)) }}
  {{-   if hasSuffix "Gi" $lim }}{{ $mib = mul $mib 1024 }}{{ end }}
  {{-   if lt $mib 288 }}
  {{-     fail (printf "crossplane-agentcore: provider.resources.memoryLimit %q is below 288Mi, the smallest limit measured to survive startup (see values.yaml)" $lim) }}
  {{-   end }}
  {{-   $_ := set $r "goMemLimit" (printf "%dMiB" (div (mul $mib 9) 10)) }}
  {{- end }}
  {{- if or $r.goMemLimit $r.goGC }}
  env:
    {{- if $r.goMemLimit }}
    - name: GOMEMLIMIT
      value: {{ $r.goMemLimit | quote }}
    {{- end }}
    {{- if $r.goGC }}
    - name: GOGC
      value: {{ $r.goGC | quote }}
    {{- end }}
  {{- end }}
  resources:
    requests:
      cpu: {{ $r.cpuRequest }}
      memory: {{ $r.memoryRequest }}
    limits:
      memory: {{ $r.memoryLimit }}
{{- end -}}
