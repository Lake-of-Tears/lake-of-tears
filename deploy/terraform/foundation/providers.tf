terraform {
  required_version = ">= 1.5.0"
  required_providers {
    null = {
      source  = "hashicorp/null"
      version = "~> 3.2"
    }
    minio = {
      source  = "aminueza/minio"
      version = "~> 3.44"
    }
  }
}

provider "minio" {
  # aminueza/minio provider speaks S3 API — works with AIStor unchanged
  minio_server   = var.aistor_endpoint
  minio_user     = var.aistor_access_key
  minio_password = var.aistor_secret_key
  minio_ssl      = false
}
