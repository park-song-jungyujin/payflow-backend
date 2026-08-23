# 값은 여기 없다. apply 후 `gcloud secrets versions add <name> --data-file=-`로 직접 채운다.
# schema-contract.md §11 시크릿 목록과 동일.
locals {
  secret_names = [
    "PAYPAL_CLIENT_ID",
    "PAYPAL_CLIENT_SECRET",
    "SLACK_SIGNING_SECRET",
    "SLACK_BOT_TOKEN",
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "SLACK_APP_CLIENT_ID",
    "SLACK_APP_CLIENT_SECRET",
  ]
}

resource "google_secret_manager_secret" "secrets" {
  for_each = toset(local.secret_names)

  project   = var.project_id
  secret_id = each.value

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}
