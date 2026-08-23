# Track C 담당자가 이 infra/ 전체를 apply할 수 있는 GCP 리소스(GCE VM / Cloud Run 등)에
# 붙일 서비스 계정. 사람 계정 로그인 대신 이 SA를 리소스에 붙이면 ADC로 자동 인증되므로
# 키 JSON 발급이 필요 없다.
#
# 이 config가 project IAM 바인딩·서비스 계정·시크릿 IAM·WIF pool까지 관리하기 때문에
# owner급 권한 묶음이 된다 — "누가 이 권한을 갖는가"가 이 프로젝트에서 가장 큰 단일
# 권한 집중이다. iam.tf의 최소 권한 원칙과 대비되는 지점이라 주석으로 남긴다.
resource "google_service_account" "terraform_admin" {
  project      = var.project_id
  account_id   = "payflow-terraform-admin"
  display_name = "infra/ 전체 terraform apply 전용 — GCP 리소스(VM/Cloud Run)에 붙여서 씀"
}

resource "google_project_iam_member" "terraform_admin_editor" {
  project = var.project_id
  role    = "roles/editor"
  member  = "serviceAccount:${google_service_account.terraform_admin.email}"
}

resource "google_project_iam_member" "terraform_admin_project_iam" {
  project = var.project_id
  role    = "roles/resourcemanager.projectIamAdmin"
  member  = "serviceAccount:${google_service_account.terraform_admin.email}"
}

resource "google_project_iam_member" "terraform_admin_service_account_admin" {
  project = var.project_id
  role    = "roles/iam.serviceAccountAdmin"
  member  = "serviceAccount:${google_service_account.terraform_admin.email}"
}

resource "google_project_iam_member" "terraform_admin_workload_identity_pool_admin" {
  project = var.project_id
  role    = "roles/iam.workloadIdentityPoolAdmin"
  member  = "serviceAccount:${google_service_account.terraform_admin.email}"
}

resource "google_project_iam_member" "terraform_admin_secretmanager_admin" {
  project = var.project_id
  role    = "roles/secretmanager.admin"
  member  = "serviceAccount:${google_service_account.terraform_admin.email}"
}

resource "google_project_iam_member" "terraform_admin_storage_admin" {
  project = var.project_id
  role    = "roles/storage.admin"
  member  = "serviceAccount:${google_service_account.terraform_admin.email}"
}

# GCS backend state 버킷은 terraform 밖(gsutil)에서 만들어서 state에 없다 — 여기서
# 명시적으로 접근 권한을 준다. 없으면 이 SA로는 terraform init조차 안 된다.
resource "google_storage_bucket_iam_member" "terraform_admin_tfstate_access" {
  bucket = "payflow-hackathon-2026-tfstate"
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.terraform_admin.email}"
}
