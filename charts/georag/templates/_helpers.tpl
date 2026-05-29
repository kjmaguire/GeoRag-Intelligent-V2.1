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
