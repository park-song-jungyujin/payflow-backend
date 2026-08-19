output "web_url" {
  value = google_cloud_run_v2_service.web.uri
}

output "api_url" {
  value = google_cloud_run_v2_service.api.uri
}

output "agent_url" {
  value = google_cloud_run_v2_service.agent.uri
}

output "agent_service_account" {
  value       = google_service_account.agent.email
  description = "IAM 콘솔에서 이 SA에 secretAccessor 바인딩이 없는 걸 보여주는 데모용."
}

output "api_service_account" {
  value = google_service_account.api.email
}

output "receipts_bucket" {
  value = google_storage_bucket.receipts.name
}

output "cloud_tasks_queue" {
  value = google_cloud_tasks_queue.payflow_tasks.name
}

output "ci_workload_identity_provider" {
  value       = google_iam_workload_identity_pool_provider.github.name
  description = "GitHub Actions 워크플로 `workload_identity_provider` 입력값."
}

output "ci_deployer_service_account" {
  value = google_service_account.deployer.email
}

output "ci_web_workload_identity_provider" {
  value       = google_iam_workload_identity_pool_provider.github_frontend.name
  description = "payflow-frontend GitHub Actions 워크플로 `workload_identity_provider` 입력값."
}

output "ci_web_deployer_service_account" {
  value = google_service_account.web_deployer.email
}
