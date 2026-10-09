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

{{/*
Fail fast on an accidentally-open GitHub auth config. GitHub is the default
provider and, unlike the pocket-id bundle, has no separate app account: if no
org/team is set, any GitHub user could log in.
*/}}
{{- define "labforge.validateAuth" -}}
{{- if and .Values.auth.enabled (eq .Values.auth.provider "github") -}}
{{- $g := .Values.global.labforge.github -}}
{{- if and (not $g.allowAll) (not $g.org) (not $g.team) (not $g.usersConfigMap) -}}
{{- fail "auth.provider=github requires a restriction: global.labforge.github.org, .team, .usersConfigMap, or allowAll=true" -}}
{{- end -}}
{{- end -}}
{{- end -}}

