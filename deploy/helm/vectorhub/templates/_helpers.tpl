{{/*
Names — resource names are stable and match the kustomize base (deploy/k8s),
so the chart and the manifests are interchangeable for a single install.
*/}}
{{- define "vectorhub.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "vectorhub.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s" (include "vectorhub.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "vectorhub.labels" -}}
app.kubernetes.io/name: {{ include "vectorhub.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{/*
Shared env-from: the non-secret ConfigMap + the credentials Secret.
*/}}
{{- define "vectorhub.envFrom" -}}
- configMapRef:
    name: vectorhub-config
- secretRef:
    name: vectorhub-secrets
{{- end -}}

{{/*
DATABASE_URL. Self-hosted: the app role's password is injected via
$(POSTGRES_APP_PASSWORD) (resolved from the env entry below it, same as the
kustomize base). External: the value carries its own credentials.
*/}}
{{- define "vectorhub.databaseUrl" -}}
{{- if .Values.postgres.externalUrl -}}
{{- .Values.postgres.externalUrl -}}
{{- else if not .Values.postgres.enabled -}}
{{- fail "postgres.externalUrl is required when postgres.enabled is false" -}}
{{- else -}}
postgresql+asyncpg://app:$(POSTGRES_APP_PASSWORD)@postgres:5432/{{ .Values.postgres.database }}
{{- end -}}
{{- end -}}

{{/*
Migrator (DDL) URL for the migrate initContainer. Self-hosted: the postgres
superuser. External: postgres.migratorUrl must be supplied explicitly.
*/}}
{{- define "vectorhub.migratorUrl" -}}
{{- if .Values.postgres.migratorUrl -}}
{{- .Values.postgres.migratorUrl -}}
{{- else if .Values.postgres.externalUrl -}}
{{- fail "postgres.migratorUrl is required when postgres.externalUrl is set (migrations need a DDL-capable URL)" -}}
{{- else -}}
postgresql+asyncpg://postgres:{{ .Values.postgres.password }}@postgres:5432/{{ .Values.postgres.database }}
{{- end -}}
{{- end -}}

{{/*
REDIS_URL. External override wins; the in-cluster Deployment is the default.
*/}}
{{- define "vectorhub.redisUrl" -}}
{{- if .Values.redis.externalUrl -}}
{{- .Values.redis.externalUrl -}}
{{- else if not .Values.redis.enabled -}}
{{- fail "redis.externalUrl is required when redis.enabled is false" -}}
{{- else -}}
redis://redis:6379/0
{{- end -}}
{{- end -}}

{{/*
Batch staging endpoint: an explicit value (S3, managed MinIO) wins; the
in-cluster MinIO Service is the default.
*/}}
{{- define "vectorhub.batchStorageEndpoint" -}}
{{- if .Values.minio.endpoint -}}
{{- .Values.minio.endpoint -}}
{{- else -}}
http://minio:9000
{{- end -}}
{{- end -}}

{{/*
The migrate initContainer: alembic upgrade head (idempotent, safe on every
pod start) + role-password bootstrap. Rendered whenever there is a DDL URL
to run against — self-hosted always, external only if migratorUrl is set.
*/}}
{{- define "vectorhub.migrateInit" -}}
{{- if or .Values.postgres.enabled .Values.postgres.migratorUrl -}}
- name: migrate
  image: "{{ .Values.image.repository }}:{{ .Values.image.tag }}"
  imagePullPolicy: {{ .Values.image.pullPolicy }}
  command: ["/usr/local/bin/migrate.sh"]
  envFrom:
    {{- include "vectorhub.envFrom" . | nindent 4 }}
  env:
    - name: MIGRATOR_DATABASE_URL
      value: {{ include "vectorhub.migratorUrl" . | quote }}
  resources:
    {{- toYaml .Values.migrate.resources | nindent 4 }}
{{- end -}}
{{- end -}}

{{/*
The per-app env entries that the ConfigMap/Secret can't express (computed
URLs that interpolate credentials from the same container's env list).
*/}}
{{- define "vectorhub.appEnv" -}}
- name: POSTGRES_APP_PASSWORD
  valueFrom:
    secretKeyRef:
      name: vectorhub-secrets
      key: POSTGRES_APP_PASSWORD
- name: DATABASE_URL
  value: {{ include "vectorhub.databaseUrl" . | quote }}
{{- end -}}
