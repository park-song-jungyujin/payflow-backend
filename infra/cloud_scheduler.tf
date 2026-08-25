# /tasks/sweep-reconcile 안전망 — /tasks/reconcile 자체 재예약 체인이 끊긴(예:
# enqueue_reconcile 실패, Cloud Tasks 유실) EXECUTING run을 주기적으로 훑는다.
# 인증은 Cloud Tasks가 쓰는 것과 같은 OIDC 패턴: api SA 신원으로 토큰을 만들어
# api 자신을 호출한다(iam.tf의 api_invoker_self가 이미 이 신원을 허용해둠).
resource "google_cloud_scheduler_job" "sweep_reconcile" {
  name      = "payflow-sweep-reconcile"
  project   = var.project_id
  region    = var.region
  schedule  = var.sweep_reconcile_schedule
  time_zone = "Etc/UTC"

  http_target {
    http_method = "POST"
    uri         = "${var.api_oidc_audience}/tasks/sweep-reconcile"

    oidc_token {
      service_account_email = google_service_account.api.email
      audience              = var.api_oidc_audience
    }
  }

  retry_config {
    retry_count = 1
  }

  depends_on = [google_project_service.apis]
}
