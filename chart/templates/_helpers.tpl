{{/* Standard chart name (used as base of resource names) */}}
{{- define "qgis.name" -}}
qgis
{{- end -}}

{{/* Common labels applied to all resources */}}
{{- define "qgis.labels" -}}
app.kubernetes.io/name: {{ include "qgis.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{/* Selector labels (a stable subset, used for matchLabels in Deployments) */}}
{{- define "qgis.selectorLabels" -}}
app.kubernetes.io/name: {{ include "qgis.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
