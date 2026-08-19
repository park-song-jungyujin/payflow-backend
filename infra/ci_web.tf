# GitHub Actions가 `payflow-frontend`(park-song-jungyujin/payflow-frontend)의 main
# 브랜치에서 `gcloud run deploy payflow-web --source .`를 돌리기 위한 keyless 인증.
# ci.tf와 같은 패턴 — 풀은 공유하고 레포별로 provider를 분리해 attribute_condition으로
# 서로의 토큰을 흉내낼 수 없게 한다. deployer SA도 분리해서 payflow-frontend CI가
# payflow-api를 배포할 actAs 권한을 갖지 않게 한다.

resource "google_iam_workload_identity_pool_provider" "github_frontend" {
  project                            = var.project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "payflow-frontend"
  display_name                       = "payflow-frontend repo"

  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
  }
  attribute_condition = "assertion.repository == 'park-song-jungyujin/payflow-frontend'"

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account" "web_deployer" {
  project      = var.project_id
  account_id   = "payflow-web-deployer"
  display_name = "GitHub Actions 배포 전용 — payflow-web Cloud Run 배포만 한다"
}

resource "google_service_account_iam_member" "web_deployer_wif_binding" {
  service_account_id = google_service_account.web_deployer.name
  role                = "roles/iam.workloadIdentityUser"
  member              = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository/park-song-jungyujin/payflow-frontend"
}

resource "google_project_iam_member" "web_deployer_run_admin" {
  project = var.project_id
  role    = "roles/run.admin"
  member  = "serviceAccount:${google_service_account.web_deployer.email}"
}

resource "google_project_iam_member" "web_deployer_cloudbuild_editor" {
  project = var.project_id
  role    = "roles/cloudbuild.builds.editor"
  member  = "serviceAccount:${google_service_account.web_deployer.email}"
}

resource "google_project_iam_member" "web_deployer_artifactregistry_writer" {
  project = var.project_id
  role    = "roles/artifactregistry.writer"
  member  = "serviceAccount:${google_service_account.web_deployer.email}"
}

resource "google_project_iam_member" "web_deployer_storage_admin" {
  # `gcloud run deploy --source`가 소스를 올리는 스테이징 버킷(run-sources-...) 쓰기용.
  project = var.project_id
  role    = "roles/storage.admin"
  member  = "serviceAccount:${google_service_account.web_deployer.email}"
}

# payflow-web 런타임 SA를 "빌려 쓰는" 권한 — 프로젝트 전체가 아니라 이 SA 하나로만 좁힌다.
resource "google_service_account_iam_member" "web_deployer_actas_web" {
  service_account_id = google_service_account.web.name
  role                = "roles/iam.serviceAccountUser"
  member              = "serviceAccount:${google_service_account.web_deployer.email}"
}

# ci.tf와 동일한 이유 — Cloud Build가 소스를 빌드할 때 쓰는 기본 Compute SA에도
# actAs가 필요하다. 이것도 이 SA 하나로만 좁혀서 부여한다.
resource "google_service_account_iam_member" "web_deployer_actas_compute_default" {
  service_account_id = "projects/${var.project_id}/serviceAccounts/${local.compute_default_sa_email}"
  role                = "roles/iam.serviceAccountUser"
  member              = "serviceAccount:${google_service_account.web_deployer.email}"
}
