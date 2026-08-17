# docs/infra/gcp-bootstrap.sh와 동일한 4종 + Secret Manager, IAM.
locals {
  required_apis = [
    "aiplatform.googleapis.com",
    "firestore.googleapis.com",
    "cloudtasks.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com", # WIF 토큰 교환 (CI)
    "sts.googleapis.com",            # WIF 토큰 교환 (CI)
    "cloudbuild.googleapis.com",     # gcloud run deploy --source
    "artifactregistry.googleapis.com",
  ]
}

resource "google_project_service" "apis" {
  for_each = toset(local.required_apis)

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}
