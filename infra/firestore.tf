# gcp-bootstrap.sh가 만든 development/deploy를 import해서 편입시킨다.
# (default) DB는 일부러 여기 안 둔다 — bootstrap.sh 기준 죽은 DB고, terraform이
# 몰라야 실수로 지우는 경로가 안 생긴다.
resource "google_firestore_database" "db" {
  for_each = toset(var.firestore_databases)

  project     = var.project_id
  name        = each.value
  location_id = var.region
  type        = "FIRESTORE_NATIVE"

  # location_id/type을 바꾸면 재생성이다 — 재생성되면 시딩된 데모 fixture가 통째로 날아간다.
  lifecycle {
    prevent_destroy = true
  }

  depends_on = [google_project_service.apis]
}
