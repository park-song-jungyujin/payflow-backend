
# 상태 파일은 GCS backend(backend.tf)에 있다. 누구든 terraform init만 하면
# 같은 state를 보고, GCS 상태 락으로 동시 apply 충돌도 막는다.
terraform {
  required_version = ">= 1.5"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}
