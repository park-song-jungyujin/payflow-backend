# GitHub Actions가 `payflow-backend`(park-song-jungyujin/payflow-backend)의 main
# 브랜치에서 `gcloud run deploy payflow-api --source .`를 돌리기 위한 keyless 인증.
# 서비스 계정 키 JSON을 GitHub Secrets에 넣지 않는다 — Workload Identity Federation으로
# 단기 토큰만 발급한다.
#
# 범위는 이 레포(payflow-backend) CI뿐이다. agent/web은 아직 코드·Dockerfile이 없어서
# CI를 만들면 첫 푸시에 바로 깨진다 — 그 레포 담당(A/B)이 준비되면 같은 패턴으로 추가.

resource "google_iam_workload_identity_pool" "github" {
  project                   = var.project_id
  workload_identity_pool_id = "github-actions"
  display_name              = "GitHub Actions"
}

resource "google_iam_workload_identity_pool_provider" "github" {
  project                            = var.project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "payflow-backend"
  display_name                       = "payflow-backend repo"

  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
  }
  # 이 레포에서 올라온 토큰만 받는다. 다른 레포/포크가 이 identity를 흉내낼 수 없다.
  attribute_condition = "assertion.repository == 'park-song-jungyujin/payflow-backend'"

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account" "deployer" {
  project      = var.project_id
  account_id   = "payflow-deployer"
  display_name = "GitHub Actions 배포 전용 — payflow-api Cloud Run 배포만 한다"
}

# GitHub Actions 워크플로가 이 SA를 사칭할 수 있게 — 위 attribute_condition으로 이미
# 레포가 좁혀져 있으니 여기서는 풀 전체를 principalSet으로 바인딩해도 안전하다.
resource "google_service_account_iam_member" "deployer_wif_binding" {
  service_account_id = google_service_account.deployer.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository/park-song-jungyujin/payflow-backend"
}

resource "google_project_iam_member" "deployer_run_admin" {
  project = var.project_id
  role    = "roles/run.admin"
  member  = "serviceAccount:${google_service_account.deployer.email}"
}

resource "google_project_iam_member" "deployer_cloudbuild_editor" {
  project = var.project_id
  role    = "roles/cloudbuild.builds.editor"
  member  = "serviceAccount:${google_service_account.deployer.email}"
}

resource "google_project_iam_member" "deployer_artifactregistry_writer" {
  project = var.project_id
  role    = "roles/artifactregistry.writer"
  member  = "serviceAccount:${google_service_account.deployer.email}"
}

resource "google_project_iam_member" "deployer_storage_admin" {
  # `gcloud run deploy --source`가 소스를 올리는 스테이징 버킷(run-sources-...) 쓰기용.
  project = var.project_id
  role    = "roles/storage.admin"
  member  = "serviceAccount:${google_service_account.deployer.email}"
}

# payflow-api 런타임 SA를 "빌려 쓰는" 권한 — 프로젝트 전체가 아니라 이 SA 하나로만 좁힌다.
resource "google_service_account_iam_member" "deployer_actas_api" {
  service_account_id = google_service_account.api.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.deployer.email}"
}

# `gcloud run deploy --source`는 배포 대상 SA(api)와 별개로, Cloud Build가 소스를
# 빌드할 때 쓰는 SA에도 actAs가 필요하다. 커스텀 빌드 SA를 지정하지 않았으므로
# 기본값인 프로젝트 기본 Compute SA가 쓰인다 — 이것도 이 SA 하나로만 좁혀서 부여한다.
resource "google_service_account_iam_member" "deployer_actas_compute_default" {
  service_account_id = "projects/${var.project_id}/serviceAccounts/${local.compute_default_sa_email}"
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.deployer.email}"
}
