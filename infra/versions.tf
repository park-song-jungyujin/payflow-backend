
# 상태 파일은 로컬(Track C 담당자 머신)에만 있고 GCS 백엔드로 옮기지 않았다.
# 다른 사람이 이 레포를 새로 clone해서 apply하면 빈 state로 시작해 payflow-api/
# payflow-queue를 새로 만들려다 "already exists"로 충돌한다. terraform은 C만 돌린다.
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
