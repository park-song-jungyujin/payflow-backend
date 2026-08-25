variable "project_id" {
  type        = string
  description = "PROJECT_ID와 동일. gcp-bootstrap.sh 참조."
}

variable "region" {
  type        = string
  default     = "asia-northeast3"
  description = "서울. Firestore는 한 번 정하면 변경 불가 (docs/infra/gcp-bootstrap.sh)."
}

variable "firestore_databases" {
  type        = list(string)
  default     = ["development", "deploy"]
  description = "gcp-bootstrap.sh가 만드는 두 Native 모드 DB. 이미 존재하면 terraform import 필요."
}

# --- Cloud Run 이미지 ---
# 아직 CI/CD가 없다. 최초 apply는 placeholder 이미지로 서비스만 만들고,
# 실제 배포는 `gcloud run deploy --image ...`로 별도 진행한다 (image는 lifecycle에서 무시).
variable "placeholder_image" {
  type    = string
  default = "us-docker.pkg.dev/cloudrun/container/hello"
}

variable "api_oidc_audience" {
  type        = string
  default     = "https://payflow-api-1095757595735.asia-northeast3.run.app"
  description = "Cloud Run이 서비스 생성 시 배정하는 안정적인 run.app URL. tasks_ping()이 검증하는 audience — 서비스를 지우고 새로 만들지 않는 한 안 바뀐다."
}

variable "agent_oidc_audience" {
  type = string
  # c9cf6d9가 "안정적인" 번호 기반 URL로 바꿨지만 apply가 안 돼서, 실제 배포된
  # agent 서비스는 지금도 이 해시 기반 URL을 자기 audience로 쓰고 있다(api의
  # AGENT_SERVICE_URL도 마찬가지). 둘 다 Cloud Run이 보장하는 안정적인 URL이라
  # 어느 쪽이든 동작하지만, 코드와 실배포를 맞추는 쪽(지금 쓰는 값)으로 되돌린다 —
  # 번호 기반 URL로 바꾸려면 api·agent 두 서비스를 동시에 재배포해야 한다.
  default     = "https://payflow-agent-6j6g3xdpma-du.a.run.app"
  description = "api_oidc_audience와 같은 이유 — agent/main.py의 /agents/*가 검증하는 audience."
}

variable "google_oauth_redirect_uri" {
  type        = string
  default     = "https://payflow-web-1095757595735.asia-northeast3.run.app/api/auth/google/callback"
  description = "auth/routes.py의 GOOGLE_OAUTH_REDIRECT_URI. Google Cloud Console에 등록한 redirect URI와 정확히 일치해야 한다."
}

variable "slack_oauth_redirect_uri" {
  type = string
  # api를 직접 가리키면 안 된다 — /auth/slack/callback은 POST 전용이라 Slack의
  # 브라우저 GET 리다이렉트를 못 받는다(405). google_oauth_redirect_uri와 같은
  # 이유로 web의 BFF 라우트를 거쳐야 한다(그 라우트가 세션 쿠키를 들고 api에
  # POST로 넘긴다).
  default     = "https://payflow-web-1095757595735.asia-northeast3.run.app/api/auth/slack/callback"
  description = "auth/slack_oauth.py의 SLACK_OAUTH_REDIRECT_URI. Slack App의 Redirect URLs와 정확히 일치해야 한다."
}

variable "agent_model" {
  type        = string
  default     = "gemini-flash-latest"
  description = "agent-tools.md — 모델 ID는 환경변수. 해커톤 규정의 최소 버전 요건을 마지막에 한 번 더 확인한다."
}

variable "gemini_model_id" {
  type        = string
  default     = "gemini-3.7-flash"
  description = "api의 영수증 파싱/검증 단발 Gemini 호출(ADK 아님)이 쓰는 모델 ID."
}

variable "gemma_model_id" {
  type        = string
  default     = "gemma-3-27b-it"
  description = "api의 에이전트 출력 영어 번역 단발 호출(guards/translate.py, ADK 아님)이 쓰는 모델 ID. Vertex AI Model Garden에 이 리전(var.vertex_location)으로 실제 호스팅되는지 배포 전 확인 필요 — 기본값은 미검증."
}

variable "vertex_location" {
  type        = string
  default     = "global"
  description = "api가 Vertex AI(Gemini structured output)를 호출할 때 쓰는 리전. Cloud Run region(var.region)과 별개."
}

# --- Cloud Run 서비스별 설정 (plan.md "Cloud Run 설정" 표) ---
variable "agent_min_instances" {
  type        = number
  default     = 0
  description = "데모 촬영 전 1로 올린다 — 콜드 스타트가 영상에 찍힌다."
}

variable "api_min_instances" {
  type        = number
  default     = 0
  description = "데모 촬영 전 1로 올린다."
}

variable "web_min_instances" {
  type    = number
  default = 0
}

variable "agent_concurrency" {
  type        = number
  default     = 4
  description = "낮게 유지 — 기본 80이면 장기 LLM 요청이 한 인스턴스에 쌓여 오토스케일이 안 뜸."
}

variable "agent_timeout_seconds" {
  type        = number
  default     = 3600
  description = "기본 5분이면 ADK 툴 루프가 잘린다. 최대 60분까지."
}

# --- api 런타임 설정 (money-safety.md 한도 · 통화) — backend/.env와 값을 맞춘다 ---
variable "app_firestore_database" {
  type        = string
  default     = "development"
  description = "payouts/store.py get_client()가 읽는 Firestore 데이터베이스 이름. firestore_databases 중 하나."
}

variable "payout_currency" {
  type        = string
  default     = "USD"
  description = "guards/routes.py, guards/limits.py가 os.environ[\"PAYOUT_CURRENCY\"]로 직접 읽는다 — 없으면 승인 자체가 KeyError로 500."
}

variable "receipt_default_currency" {
  type    = string
  default = "KRW"
}

variable "max_amount_per_item_minor" {
  type        = number
  default     = 200000
  description = "1500 KRW/USD 가정으로 기존 KRW 캡(3,000,000)을 USD로 환산."
}

variable "max_amount_per_batch_minor" {
  type        = number
  default     = 333333
  description = "1500 KRW/USD 가정으로 기존 KRW 캡(5,000,000)을 USD로 환산."
}

variable "max_amount_monthly_minor" {
  type        = number
  default     = 666667
  description = "1500 KRW/USD 가정으로 기존 KRW 캡(10,000,000)을 USD로 환산."
}

variable "approval_token_ttl_seconds" {
  type    = number
  default = 600
}

variable "payout_max_reconcile_attempts" {
  type    = number
  default = 5
}

variable "reconcile_delay_seconds" {
  type        = number
  default     = 30
  description = "payouts/tasks_queue.py enqueue_reconcile()이 /tasks/reconcile을 예약하는 지연 시간. 샌드박스에서 PayPal이 배치를 처리할 시간을 준다."
}

variable "sweep_reconcile_schedule" {
  type        = string
  default     = "*/5 * * * *"
  description = "cloud_scheduler.tf가 /tasks/sweep-reconcile을 부르는 주기. /tasks/reconcile 자체 재예약 체인이 끊긴 EXECUTING run을 잡아내는 안전망 주기다."
}

variable "claim_request_ttl_seconds" {
  type        = number
  default     = 86400
  description = "src/ingest/store.py, src/settlements/store.py가 claim_request 만료 시각을 계산할 때 쓰는 TTL."
}

variable "reminder_delay_seconds" {
  type        = number
  default     = 20
  description = "청구 재요청(ReminderReason) 알림을 예약하는 지연 시간."
}

# --- Cloud Tasks 재시도 (plan.md: "재시도를 만드는 건 Cloud Run이 아니라 큐다") ---
variable "task_max_attempts" {
  type    = number
  default = 5
}

variable "task_min_backoff_seconds" {
  type    = number
  default = 5
}

variable "task_max_backoff_seconds" {
  type    = number
  default = 60
}
