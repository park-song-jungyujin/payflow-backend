# 영수증 원본 이미지 + 파싱 원문(raw_text) 저장용. run-sources-... 버킷은 Cloud Run
# 소스 배포 전용이라 재사용 불가 (재훈 요청, 2026-08-19).
resource "google_storage_bucket" "receipts" {
  name                        = "${var.project_id}-receipts"
  project                     = var.project_id
  location                    = var.region
  uniform_bucket_level_access = true

  depends_on = [google_project_service.apis]
}

# api SA만 쓴다 — agent는 절대 규칙 1과 같은 이유로 원본 접근 권한을 안 준다
# (여기서 막을 필요는 없지만, 최소 권한 원칙을 파일/버킷 자원에도 동일하게 적용).
resource "google_storage_bucket_iam_member" "api_receipts_write" {
  bucket = google_storage_bucket.receipts.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.api.email}"
}
