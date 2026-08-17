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
  type        = string
  default     = "https://payflow-agent-6j6g3xdpma-du.a.run.app"
  description = "api_oidc_audience와 같은 이유 — agent/main.py의 /agents/*가 검증하는 audience."
}

variable "agent_model" {
  type        = string
  default     = "gemini-flash-latest"
  description = "agent-tools.md — 모델 ID는 환경변수. 해커톤 규정의 최소 버전 요건을 마지막에 한 번 더 확인한다."
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
