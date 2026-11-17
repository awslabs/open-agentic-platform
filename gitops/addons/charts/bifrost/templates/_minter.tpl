{{- /* Pod spec shared by the minter Job and CronJob (templates/vk-minter.yaml). */ -}}
{{- define "bifrost.minterPod" }}
serviceAccountName: bifrost-vk-minter
restartPolicy: OnFailure
containers:
  - name: minter
    image: {{ .jk.image | default "python:3.12-alpine" | quote }}
    command: ["python", "/scripts/vk-minter.py"]
    env:
      - name: BIFROST_URL
        value: {{ .url | quote }}
      - name: BIFROST_ADMIN_USERNAME
        valueFrom:
          secretKeyRef: {name: {{ .adminSecret | quote }}, key: {{ .userKey | quote }}}
      - name: BIFROST_ADMIN_PASSWORD
        valueFrom:
          secretKeyRef: {name: {{ .adminSecret | quote }}, key: {{ .passKey | quote }}}
      - name: TARGET_NAMESPACE
        value: {{ .jk.targetNamespace | default "agent-evaluation" | quote }}
      - name: SECRET_NAME
        value: {{ .jk.secretName | default "agent-evaluation-harness-judge-key" | quote }}
      - name: JUDGE_MODELS
        value: {{ .jk.models | default (list "judge-default") | toJson | quote }}
      - name: JUDGE_BUDGET_USD
        value: {{ .budget.maxLimitUsd | default 20 | quote }}
      - name: JUDGE_BUDGET_RESET
        value: {{ .budget.resetDuration | default "1M" | quote }}
      - name: JUDGE_RATE_LIMIT_REQUESTS
        value: {{ .rl.requestMaxLimit | default 2000 | quote }}
      - name: JUDGE_RATE_LIMIT_RESET
        value: {{ .rl.requestResetDuration | default "1h" | quote }}
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
    configMap: {name: bifrost-vk-minter}
{{- end }}
