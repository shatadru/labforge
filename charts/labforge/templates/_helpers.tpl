{{- define "labforge.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "labforge.fullname" -}}
{{- printf "%s-%s" .Release.Name (include "labforge.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "labforge.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "labforge.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "labforge.labels" -}}
app.kubernetes.io/name: {{ include "labforge.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/* Fullname of the bundled oauth2-proxy subchart (mirrors its helper). */}}
{{- define "labforge.auth.fullname" -}}
{{- printf "%s-oauth2-proxy" .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Fullname of the bundled pocket-id subchart, and its operator Secret. */}}
{{- define "labforge.idp.fullname" -}}
{{- printf "%s-pocket-id" .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "labforge.authSecretName" -}}
{{- .Values.auth.existingSecret | default "labforge-auth" -}}
{{- end -}}

{{/* Fullname of the bundled ntfy subchart. */}}
{{- define "labforge.chat.fullname" -}}
{{- printf "%s-ntfy" .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

