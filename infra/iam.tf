# 절대 규칙 1 (CLAUDE.md): agent 서비스 계정은 PayPal 자격증명에 접근하지 않는다.
# 코드가 아니라 여기, IAM으로 막는다. 아래 secret_names 중 어느 것도 agent SA에
# secretAccessor를 부여하는 바인딩이 없다 — 그게 증명이다.

# --- Firestore ---
# agent는 Firestore를 직접 안 쓴다 (architecture.md: api가 제공하는 툴을 통해서만).
# 그래서 datastore.user는 api SA에만 부여한다.
resource "google_project_iam_member" "api_datastore" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.api.email}"
}

# --- Vertex AI ---
# api: 영수증 파싱용 Gemini 단발 호출 (architecture.md). agent: ADK 세션 전체.
resource "google_project_iam_member" "api_vertex" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.api.email}"
}

resource "google_project_iam_member" "agent_vertex" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.agent.email}"
}

# --- Cloud Tasks ---
# 큐에 태스크를 넣는 건 api뿐이다.
resource "google_project_iam_member" "api_tasks_enqueuer" {
  project = var.project_id
  role    = "roles/cloudtasks.enqueuer"
  member  = "serviceAccount:${google_service_account.api.email}"
}

# --- Secret Manager: PayPal + Slack 시크릿은 api SA에만 ---
resource "google_secret_manager_secret_iam_member" "api_secret_access" {
  # secret_names(local, secrets.tf)를 도는 것 — 리소스 attribute map을 돌면 plan 시점에
  # google_secret_manager_secret.secrets가 아직 생성 전이라 "known only after apply" 에러.
  for_each = toset(local.secret_names)

  project   = var.project_id
  secret_id = google_secret_manager_secret.secrets[each.value].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.api.email}"
}

# --- Cloud Run invoker ---
# web은 공개(브라우저가 직접 침). api도 공개(Slack webhook + web BFF).
# agent는 비공개 — Cloud Tasks가 api SA로 OIDC 토큰을 만들어 부르는 경로만 허용한다.
resource "google_cloud_run_v2_service_iam_member" "web_public" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.web.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

resource "google_cloud_run_v2_service_iam_member" "api_public" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.api.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

resource "google_cloud_run_v2_service_iam_member" "agent_invoker_api_only" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.agent.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.api.email}"
}

# api도 자기 자신의 /tasks/* 콜백(execute-payout, reconcile 등)을 Cloud Tasks로
# 부른다 — 같은 OIDC 신원(api SA)으로 자기 자신을 호출할 수 있어야 한다.
resource "google_cloud_run_v2_service_iam_member" "api_invoker_self" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.api.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.api.email}"
}
