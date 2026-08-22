# 영수증 원본 이미지 + 파싱 원문(raw_text) 저장용. run-sources-... 버킷은 Cloud Run
# 소스 배포 전용이라 재사용 불가 (재훈 요청, 2026-08-19).
resource "google_storage_bucket" "receipts" {
  name                        = "${var.project_id}-receipts"
  project                     = var.project_id
  location                    = var.region
  uniform_bucket_level_access = true

  depends_on = [google_project_service.apis]
}

# 쓰기는 api SA만 한다.
resource "google_storage_bucket_iam_member" "api_receipts_write" {
  bucket = google_storage_bucket.receipts.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.api.email}"
}

# 청구자 에이전트는 파싱 원문을 GCS에서 직접 읽는다 — schema-contract.md §9의
# 입력 계약이 원문 자체가 아니라 `raw_text_gcs_uri`다. 원문을 Cloud Tasks 본문에
# 실으면 큐에 영속화되고 로그에 남으므로(§2가 막아둔 경로) URI만 보내고 읽기
# 권한을 여기서 연다.
#
# **이미지는 여전히 api SA만 읽는다.** IAM 조건으로 `raw_text/` 프리픽스에
# 한정한다(키 형식은 src/parsing/storage.py의 raw_text_key). 버킷 단위로 열면
# 에이전트가 영수증 이미지 원본까지 읽게 되는데, 이미지↔파싱 결과 검증은 api의
# 단발 Gemini 호출 몫이지 에이전트 몫이 아니다.
#
# 절대 규칙 1과 충돌하지 않는다 — 그건 PayPal 자격증명 얘기고, 여기서 열리는
# 건 에이전트가 판단 근거로 삼아야 하는 비신뢰 텍스트뿐이다.
# 배포 순서: 이 바인딩 apply → agent 배포 (역순이면 에이전트가 원문을 못 읽는다).
resource "google_storage_bucket_iam_member" "agent_receipts_raw_text_read" {
  bucket = google_storage_bucket.receipts.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.agent.email}"

  condition {
    title       = "raw_text_objects_only"
    description = "파싱 원문(raw_text/)만. 영수증 이미지(images/)는 제외한다."
    expression  = "resource.name.startsWith(\"projects/_/buckets/${google_storage_bucket.receipts.name}/objects/raw_text/\")"
  }
}
