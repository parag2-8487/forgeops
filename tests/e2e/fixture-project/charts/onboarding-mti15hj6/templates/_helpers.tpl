{{- define "onboarding-mti15hj6.fullname" -}}
{{- printf "%s-%s" .Release.Name "onboarding-mti15hj6" | trunc 63 | trimSuffix "-" -}}
{{- end -}}
