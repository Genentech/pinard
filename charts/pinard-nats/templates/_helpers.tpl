{{- define "pinard-nats.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "pinard-nats.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "pinard-nats.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{ include "pinard-nats.selectorLabels" . }}
app.kubernetes.io/version: {{ .Values.image.tag | default .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "pinard-nats.selectorLabels" -}}
app.kubernetes.io/name: {{ include "pinard-nats.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/* Name of the Secret holding NATS user passwords (chart-generated, Vault-backed, or pre-existing). */}}
{{- define "pinard-nats.authSecretName" -}}
{{- if .Values.existingSecret }}{{ .Values.existingSecret }}{{ else }}{{ printf "%s-auth" (include "pinard-nats.fullname" .) }}{{ end }}
{{- end }}

{{/* Turn an account name into an env-var-safe suffix (UPPER, non-alnum -> _). */}}
{{- define "pinard-nats.envName" -}}
{{- . | upper | regexReplaceAll "[^A-Z0-9]" "_" }}
{{- end }}
