{{- define "vp.labels" -}}
app.kubernetes.io/part-of: voltpath
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}

{{- define "vp.env" -}}
- { name: ENV, value: {{ .Values.env | quote }} }
- { name: KAFKA_BOOTSTRAP, value: {{ .Values.endpoints.kafkaBootstrap | quote }} }
- { name: REDIS_URL, value: {{ .Values.endpoints.redisUrl | quote }} }
- { name: S3_ENDPOINT, value: {{ .Values.endpoints.s3Endpoint | quote }} }
- { name: LAKE_BUCKET, value: {{ .Values.endpoints.lakeBucket | quote }} }
- { name: JWT_PRIVATE_KEY_PATH, value: /secrets/jwt_private.pem }
- { name: MODEL_DIR, value: /models }
{{- end }}

{{- define "vp.envFrom" -}}
- secretRef: { name: voltpath-secrets }
{{- end }}

{{- define "vp.securityContext" -}}
securityContext:
  runAsNonRoot: {{ .Values.podSecurity.runAsNonRoot }}
  runAsUser: {{ .Values.podSecurity.runAsUser }}
  readOnlyRootFilesystem: {{ .Values.podSecurity.readOnlyRootFilesystem }}
  allowPrivilegeEscalation: false
  capabilities: { drop: ["ALL"] }
  seccompProfile: { type: RuntimeDefault }
{{- end }}

{{- define "vp.volumes" -}}
- name: secrets
  secret: { secretName: voltpath-secrets, items: [{ key: jwt_private.pem, path: jwt_private.pem }] }
- name: tmp
  emptyDir: {}
- name: models
  persistentVolumeClaim: { claimName: voltpath-models }
{{- end }}

{{- define "vp.mounts" -}}
- { name: secrets, mountPath: /secrets, readOnly: true }
- { name: tmp, mountPath: /tmp }
- { name: models, mountPath: /models }
{{- end }}
