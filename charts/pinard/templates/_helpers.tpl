{{/* Release-scoped base name (HelmRelease releaseName, e.g. pinard-uat). */}}
{{- define "pinard.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Common labels. Call with a dict: (dict "root" $ "component" "website"). */}}
{{- define "pinard.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .root.Chart.Name .root.Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
app.kubernetes.io/name: pinard-{{ .component }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
app.kubernetes.io/part-of: pinard
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
{{- end -}}

{{/* Selector labels. Call with (dict "root" $ "component" "website"). */}}
{{- define "pinard.selectorLabels" -}}
app.kubernetes.io/name: pinard-{{ .component }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}
