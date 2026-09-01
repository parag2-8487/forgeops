terraform {
  required_version = ">= 1.6.0"

  required_providers {
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.30"
    }
  }
}

variable "namespace" {
  description = "Namespace to deploy onboarding-mti15hj6 into."
  type        = string
  default     = "onboarding-mti15hj6"
}

variable "image" {
  description = "Fully qualified image reference for onboarding-mti15hj6."
  type        = string
  default     = "onboarding-mti15hj6:latest"
}

variable "replicas" {
  description = "Number of pod replicas."
  type        = number
  default     = 1
}

variable "container_port" {
  description = "Port the container listens on."
  type        = number
  default     = 3000
}

resource "kubernetes_namespace" "app" {
  metadata {
    name = var.namespace
  }
}

resource "kubernetes_deployment" "app" {
  metadata {
    name      = "onboarding-mti15hj6"
    namespace = kubernetes_namespace.app.metadata[0].name
  }

  spec {
    replicas = var.replicas

    selector {
      match_labels = {
        "app.kubernetes.io/name" = "onboarding-mti15hj6"
      }
    }

    template {
      metadata {
        labels = {
          "app.kubernetes.io/name" = "onboarding-mti15hj6"
        }
      }

      spec {
        security_context {
          run_as_non_root = true
          run_as_user     = 1001
        }

        container {
          name  = "onboarding-mti15hj6"
          image = var.image

          port {
            container_port = var.container_port
          }

          security_context {
            allow_privilege_escalation = false
          }
        }
      }
    }
  }
}

output "namespace" {
  description = "Namespace onboarding-mti15hj6 was deployed into."
  value       = kubernetes_namespace.app.metadata[0].name
}
