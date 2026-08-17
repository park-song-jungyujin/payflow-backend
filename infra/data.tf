# google_compute_default_service_account 데이터소스는 Compute Engine API 활성화를
# 요구한다 (이 프로젝트는 안 씀 — Cloud Run만 쓴다). SA 자체는 이미 존재하므로 project
# number로 이메일만 조립한다.
data "google_project" "current" {
  project_id = var.project_id
}

locals {
  compute_default_sa_email = "${data.google_project.current.number}-compute@developer.gserviceaccount.com"
}
