{{/* Common naming + label helpers. */}}
{{- define "georag.fullname" -}}
{{- printf "%s" (include "common.fullname" .) | default .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "georag.labels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" -}}
{{- end -}}

{{- define "georag.image" -}}
{{- $tag := default .Values.global.imageTag .image.tag -}}
{{- printf "%s/%s/%s:%s" .Values.global.imageRegistry .Values.global.imageRepoOwner .image.repository $tag -}}
{{- end -}}
