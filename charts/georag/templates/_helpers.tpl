{{/* Common chart helpers — §11.6 doc-phase 180 */}}

{{/*
Resolve the image reference. Prepends global.imageRegistry if set
(air-gap installs override this to point at the customer's mirror).

Usage: {{ include "georag.image" (dict "repo" .Values.postgresql.image.repository "tag" .Values.postgresql.image.tag "global" .Values.global) }}
*/}}
{{- define "georag.image" -}}
{{- $registry := default "" .global.imageRegistry -}}
{{- if $registry -}}
{{ $registry }}/{{ .repo }}:{{ .tag }}
{{- else -}}
{{ .repo }}:{{ .tag }}
{{- end -}}
{{- end -}}

{{/*
Common labels — applied to every resource. Standard `app.kubernetes.io/*`
labels per the K8s recommended set.
*/}}
{{- define "georag.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/*
Component-specific selector labels.

Usage: {{ include "georag.selectorLabels" (dict "component" "postgresql" "root" .) }}
*/}}
{{- define "georag.selectorLabels" -}}
app.kubernetes.io/name: {{ .root.Chart.Name }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{/*
Resolve the storageClassName for a PVC. Falls back to
global.storageClass if the per-service value is empty.
*/}}
{{- define "georag.storageClass" -}}
{{- $svcStorage := default "" .svcStorageClass -}}
{{- $globalStorage := default "" .global.storageClass -}}
{{- if $svcStorage -}}
{{ $svcStorage }}
{{- else if $globalStorage -}}
{{ $globalStorage }}
{{- end -}}
{{- end -}}

{{/*
Pull-secrets reference — emits the imagePullSecrets list block if
configured, otherwise nothing.
*/}}
{{- define "georag.imagePullSecrets" -}}
{{- if .Values.global.imagePullSecrets -}}
imagePullSecrets:
{{- range .Values.global.imagePullSecrets }}
- name: {{ . }}
{{- end }}
{{- end -}}
{{- end -}}

{{/*
Environment shared by every process that runs the laravel image: Octane,
Horizon, Reverb and the schema Job. One definition, so the three long-lived
processes cannot drift apart (they share a codebase, and a variable set on two
of the three is nearly always an omission on the third).

Usage: env:
         {{- include "georag.laravelEnv" . | nindent 12 }}

- QUEUE_CONNECTION / CACHE_STORE / SESSION_DRIVER = redis. Laravel's defaults
  are all `database`, and the first is the quiet one: Horizon supervises only
  Redis queues, so with the database driver every chat job is written to a
  table nothing drains and both supervisors sit idle and healthy.
- APP_KEY: the Secret's key is LARAVEL_APP_KEY, so a pod that takes the Secret
  by envFrom has no APP_KEY at all.
- APP_URL: config/app.php falls back to http://localhost, which makes
  `localhost` the only stateful Sanctum domain and breaks the SPA login on any
  other host.
*/}}
{{- define "georag.laravelEnv" -}}
- name: APP_ENV
  value: production
- name: APP_URL
  value: {{ printf "%s://%s" (ternary "https" "http" .Values.ingress.tls.enabled) .Values.ingress.host | quote }}
- name: APP_KEY
  valueFrom:
    secretKeyRef:
      name: {{ .Release.Name }}-secrets
      key: LARAVEL_APP_KEY
# SEC-1/SEC-2 (2026-09-29): Laravel runs as georag_app
# (NOSUPERUSER NOBYPASSRLS, provisioned by the pg-init Job)
# and talks to Postgres DIRECTLY, as on AWS. A superuser
# bypasses RLS even under FORCE ROW LEVEL SECURITY, and
# BindWorkspaceRlsContext's session-scoped binding is unsound
# behind PgBouncer's transaction pooling (it refuses to serve
# when DB_POOLED is true). No Laravel connection is configured
# with the superuser; migrations are the schema Job's.
- name: DB_CONNECTION
  value: pgsql
- name: DB_HOST
  value: {{ .Release.Name }}-postgresql
- name: DB_PORT
  value: "5432"
- name: DB_POOLED
  value: "false"
- name: DB_DATABASE
  value: georag
- name: DB_USERNAME
  value: georag_app
- name: DB_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ .Release.Name }}-secrets
      key: PG_APP_PASSWORD
- name: REDIS_HOST
  value: {{ .Release.Name }}-redis
- name: REDIS_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ .Release.Name }}-secrets
      key: REDIS_PASSWORD
- name: QUEUE_CONNECTION
  value: redis
- name: CACHE_STORE
  value: redis
- name: SESSION_DRIVER
  value: redis
{{- end -}}

{{/*
What Octane and Horizon need to PUBLISH a broadcast event to Reverb; empty
while Reverb is off. Horizon needs it as much as Octane does: the queued jobs
behind a query dispatch QueryStreamEvent.

BROADCAST_CONNECTION is stated rather than left to config/broadcasting.php,
which picks `reverb` only when REVERB_APP_KEY is set: a deploy that dropped it
once produced green health checks and a chat UI that hung on every query
(2026-08-11). REVERB_HOST is the in-cluster Service, not the public host — the
publish is a server-to-server call.
*/}}
{{- define "georag.reverbPublisherEnv" -}}
{{- if .Values.laravelReverb.enabled }}
- name: BROADCAST_CONNECTION
  value: reverb
- name: REVERB_APP_ID
  valueFrom:
    secretKeyRef:
      name: {{ .Release.Name }}-secrets
      key: REVERB_APP_ID
- name: REVERB_APP_KEY
  valueFrom:
    secretKeyRef:
      name: {{ .Release.Name }}-secrets
      key: REVERB_APP_KEY
- name: REVERB_APP_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ .Release.Name }}-secrets
      key: REVERB_APP_SECRET
- name: REVERB_HOST
  value: {{ .Release.Name }}-laravel-reverb
- name: REVERB_PORT
  value: {{ .Values.laravelReverb.service.port | quote }}
- name: REVERB_SCHEME
  value: http
{{- end }}
{{- end -}}

{{/*
The Hatchet client settings every process that talks to the engine needs: the
fastapi pods (they trigger workflows) and the worker.

HATCHET_CLIENT_HOST_PORT, not HATCHET_CLIENT_HOST: the SDK's settings class
reads the former and silently ignores the latter, falling back to
localhost:7070. HATCHET_CLIENT_TLS_STRATEGY=none because the engine's gRPC
listener is plaintext (SERVER_GRPC_INSECURE) and the SDK's own default is
`tls`, which against a plaintext listener is UNAVAILABLE at registration, not
a warning.
*/}}
{{- define "georag.hatchetClientEnv" -}}
- name: HATCHET_CLIENT_TOKEN
  valueFrom:
    secretKeyRef:
      name: {{ .Release.Name }}-secrets
      key: HATCHET_CLIENT_TOKEN
- name: HATCHET_CLIENT_HOST_PORT
  value: {{ printf "%s-hatchet:%v" .Release.Name .Values.hatchet.engine.service.grpcPort | quote }}
- name: HATCHET_CLIENT_TLS_STRATEGY
  value: none
{{- end -}}

{{/*
Where the python pods (fastapi and the Hatchet worker) find the model
sidecars. Setting a URL makes the pod proxy to the shared copy instead of
loading its own; RERANKER_BACKEND defaults to `bedrock` in code, which an
on-prem install cannot call, so it is stated whenever the reranker sidecar is
the intended reranker. EMBEDDING_MODEL_NAME/REVISION come from the same value
the embedding sidecar uses, so the three cannot disagree.
*/}}
{{- define "georag.modelServiceEnv" -}}
{{- with .Values.modelSidecars.embedding }}
{{- if .enabled }}
- name: EMBEDDING_SERVICE_URL
  value: {{ printf "http://%s-embedding:%v" $.Release.Name .service.port | quote }}
- name: EMBEDDING_MODEL_NAME
  value: {{ .model | quote }}
- name: EMBEDDING_MODEL_REVISION
  value: {{ .revision | quote }}
{{- end }}
{{- end }}
{{- with .Values.modelSidecars.sparse }}
{{- if .enabled }}
- name: SPARSE_SERVICE_URL
  value: {{ printf "http://%s-sparse:%v" $.Release.Name .service.port | quote }}
{{- end }}
{{- end }}
{{- with .Values.modelSidecars.reranker }}
{{- if .enabled }}
- name: RERANKER_BACKEND
  value: {{ .backend | quote }}
- name: RERANKER_SERVICE_URL
  value: {{ printf "http://%s-reranker:%v" $.Release.Name .service.port | quote }}
{{- end }}
{{- end }}
{{- end -}}
