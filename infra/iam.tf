# 절대 규칙 1 (CLAUDE.md): agent 서비스 계정은 PayPal 자격증명에 접근하지 않는다.
# 코드가 아니라 여기, IAM으로 막는다. 아래 secret_names 중 어느 것도 agent SA에
# secretAccessor를 부여하는 바인딩이 없다 — 그게 증명이다.

# --- Firestore ---
# agent는 Firestore를 원칙적으로 직접 안 쓴다 (architecture.md: api가 제공하는 툴을
# 통해서만). 그래서 datastore.user는 기본적으로 api SA에만 부여한다.
resource "google_project_iam_member" "api_datastore" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.api.email}"
}

# 예외 하나 — agent_sessions 컬렉션(청구자·집행자 세션 이어가기, shared/memory.py).
# schema-contract.md §2 "IAM 한계": Firestore Admin SDK는 Security Rules를 우회하므로
# 이 권한은 컬렉션 단위로 못 좁힌다 — agent SA는 기술적으로 다른 컬렉션도 읽고 쓸 수
# 있게 된다. "agent_sessions만 쓴다"는 코드 컨벤션과 리뷰로 지키는 경계이지 IAM이
# 강제하는 경계가 아니다. 배포 순서: 이 바인딩 apply → agent 배포 (역순이면 agent가
# PermissionDenied로 즉시 실패한다 — 조용한 실패는 없다).
resource "google_project_iam_member" "agent_datastore" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.agent.email}"
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

# Cloud Tasks가 태스크 실행 시 api SA 신원의 OIDC 토큰을 만들어 /tasks/* 콜백을
# 부르게 하려면, Cloud Tasks 서비스 에이전트가 api SA에 대해 토큰을 발급할 권한이
# 있어야 한다 (api SA를 "사칭"하는 게 아니라 그 신원의 OIDC 토큰만 만드는 것).
resource "google_service_account_iam_member" "cloudtasks_can_mint_api_oidc" {
  service_account_id = google_service_account.api.name
  role                = "roles/iam.serviceAccountTokenCreator"
  member              = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-cloudtasks.iam.gserviceaccount.com"
}

# CreateTask 호출 자체(= api SA 본인)도 태스크에 넣는 oidc_token.service_account_email
# 대상(api SA 자기 자신)에 대해 actAs 권한이 있어야 한다 — 위 바인딩과 별개 요건.
resource "google_service_account_iam_member" "api_can_actas_self_for_tasks" {
  service_account_id = google_service_account.api.name
  role                = "roles/iam.serviceAccountUser"
  member              = "serviceAccount:${google_service_account.api.email}"
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

# Cloud Run 서비스 에이전트도 배포 시점에 시크릿 존재/접근을 확인한다 —
# api SA 바인딩과 별개로 이게 없으면 "version latest was not found"로 apply가 실패한다.
resource "google_secret_manager_secret_iam_member" "run_service_agent_secret_access" {
  for_each = toset(local.secret_names)

  project   = var.project_id
  secret_id = google_secret_manager_secret.secrets[each.value].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:service-${data.google_project.current.number}@serverless-robot-prod.iam.gserviceaccount.com"
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
