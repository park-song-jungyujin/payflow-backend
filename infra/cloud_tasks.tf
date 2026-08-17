# CLOUD_TASKS_QUEUE 환경변수에 이 이름을 넣는다. 이미 gcloud로 만들어진 큐라 이름을 맞춰
# import한다 — 재시도 설정(max_attempts 100, backoff 0.1s~3600s)은 기본값 그대로였던 걸
# plan.md 요구대로 명시값으로 좁힌다. 이 diff는 의도한 변경이지 replace가 아니다.
resource "google_cloud_tasks_queue" "payflow_tasks" {
  name     = "payflow-queue"
  project  = var.project_id
  location = var.region

  retry_config {
    max_attempts  = var.task_max_attempts
    min_backoff   = "${var.task_min_backoff_seconds}s"
    max_backoff   = "${var.task_max_backoff_seconds}s"
    max_doublings = 3
  }

  rate_limits {
    max_concurrent_dispatches = 10
    max_dispatches_per_second = 5
  }

  depends_on = [google_project_service.apis]
}
