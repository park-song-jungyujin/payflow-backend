resource "google_service_account" "web" {
  project      = var.project_id
  account_id   = "payflow-web"
  display_name = "Payflow web (Next.js BFF) — 시크릿 없음"
}

resource "google_service_account" "api" {
  project      = var.project_id
  account_id   = "payflow-api"
  display_name = "Payflow api — PayPal/Slack 시크릿, Firestore 쓰기 유일 창구"
}

resource "google_service_account" "agent" {
  project      = var.project_id
  account_id   = "payflow-agent"
  display_name = "Payflow agent (ADK) — PayPal 접근 불가. Vertex AI만 호출"
}

resource "google_cloud_run_v2_service" "web" {
  name     = "payflow-web"
  project  = var.project_id
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.web.email
    scaling {
      min_instance_count = var.web_min_instances
    }
    containers {
      image = var.placeholder_image
      env {
        # agent와 같은 이유로 상수 고정(var.api_oidc_audience) — 자기 자신의 .uri를
        # 참조하면 순환 참조가 난다. web의 승인 프록시(route.ts)가 이 값으로 api를 부른다.
        name  = "API_BASE_URL"
        value = var.api_oidc_audience
      }
    }
  }

  lifecycle {
    # client/client_version/build_config/scaling(top-level)은 `gcloud run deploy`가
    # 배포마다 찍는 필드라 Terraform 선언과 항상 어긋난다 — 무시한다.
    ignore_changes = [
      template[0].containers[0].image,
      client,
      client_version,
      build_config,
      scaling,
    ]
  }

  depends_on = [google_project_service.apis]
}

resource "google_cloud_run_v2_service" "api" {
  name     = "payflow-api"
  project  = var.project_id
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL" # Slack webhook + web BFF가 외부에서 친다. 인가는 서명검증/토큰이 한다.

  template {
    # 2단계 마이그레이션 2단계: default compute SA(roles/editor)에서 전용 SA로 전환.
    # 1단계(import 시점엔 local.compute_default_sa_email로 맞춰 diff 없이 편입)는 끝났다.
    service_account = google_service_account.api.email
    timeout         = "300s"
    scaling {
      min_instance_count = var.api_min_instances
      max_instance_count = 20
    }
    containers {
      image = var.placeholder_image
      env {
        name  = "GCP_PROJECT"
        value = var.project_id
      }
      env {
        name  = "CLOUD_TASKS_QUEUE"
        value = google_cloud_tasks_queue.payflow_tasks.name
      }
      env {
        name  = "CLOUD_TASKS_LOCATION"
        value = var.region
      }
      env {
        # 이미 배포된 서비스가 쓰던 값. Cloud Run v2는 env 목록을 통째로 갈아치우므로
        # 여기 빠지면 import 후 apply에서 지워진다 → main.py의 os.environ["OIDC_AUDIENCE"]가
        # KeyError → /tasks/* 전부 500. 자기 자신의 .uri를 참조하면 순환 참조가 나서 상수로 고정한다.
        name  = "OIDC_AUDIENCE"
        value = var.api_oidc_audience
      }
      env {
        name  = "TASKS_SERVICE_ACCOUNT_EMAIL"
        value = google_service_account.api.email
      }
      env {
        name  = "GCS_RECEIPTS_BUCKET"
        value = google_storage_bucket.receipts.name
      }
      env {
        name  = "FIRESTORE_DATABASE"
        value = var.app_firestore_database
      }
      env {
        # guards/routes.py, guards/limits.py가 os.environ["PAYOUT_CURRENCY"]로 직접
        # 읽는다(기본값 없음) — 여기 빠지면 approve 전체가 KeyError로 500.
        name  = "PAYOUT_CURRENCY"
        value = var.payout_currency
      }
      env {
        name  = "RECEIPT_DEFAULT_CURRENCY"
        value = var.receipt_default_currency
      }
      env {
        name  = "MAX_AMOUNT_PER_ITEM_MINOR"
        value = tostring(var.max_amount_per_item_minor)
      }
      env {
        name  = "MAX_AMOUNT_PER_BATCH_MINOR"
        value = tostring(var.max_amount_per_batch_minor)
      }
      env {
        name  = "MAX_AMOUNT_MONTHLY_MINOR"
        value = tostring(var.max_amount_monthly_minor)
      }
      env {
        name  = "APPROVAL_TOKEN_TTL_SECONDS"
        value = tostring(var.approval_token_ttl_seconds)
      }
      env {
        name  = "PAYOUT_MAX_RECONCILE_ATTEMPTS"
        value = tostring(var.payout_max_reconcile_attempts)
      }
      env {
        name  = "RECONCILE_DELAY_SECONDS"
        value = tostring(var.reconcile_delay_seconds)
      }
      dynamic "env" {
        for_each = local.secret_names
        content {
          name = env.value
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.secrets[env.value].secret_id
              version = "latest"
            }
          }
        }
      }
    }
  }

  lifecycle {
    # client/client_version/build_config/scaling(top-level)은 `gcloud run deploy`가
    # 배포마다 찍는 필드라 Terraform 선언과 항상 어긋난다 — 무시한다.
    ignore_changes = [
      template[0].containers[0].image,
      client,
      client_version,
      build_config,
      scaling,
    ]
  }

  depends_on = [
    google_project_service.apis,
    google_secret_manager_secret_iam_member.api_secret_access,
    google_secret_manager_secret_iam_member.run_service_agent_secret_access,
  ]
}

# agent는 api가 Cloud Tasks로만 부른다. VPC 커넥터는 안 쓴다 (plan.md: 설정에 반나절 날아간다).
# 네트워크는 열어두고 IAM으로 잠근다 — invoker를 api SA로만 제한한다 (iam.tf).
resource "google_cloud_run_v2_service" "agent" {
  name     = "payflow-agent"
  project  = var.project_id
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account                  = google_service_account.agent.email
    timeout                          = "${var.agent_timeout_seconds}s"
    max_instance_request_concurrency = var.agent_concurrency
    scaling {
      min_instance_count = var.agent_min_instances
    }
    containers {
      image = var.placeholder_image
      env {
        # api와 동일한 이유로 상수 고정 — 자기 자신의 .uri를 참조하면 순환 참조가 난다.
        # main.py의 /agents/*가 검증하는 audience(Cloud Tasks가 이 값으로 토큰을 만든다).
        name  = "OIDC_AUDIENCE"
        value = var.agent_oidc_audience
      }
      env {
        # agent가 agent_drafts를 쓰려고 되부르는 api 주소. api는 allUsers invoker라
        # 별도 run.invoker 바인딩 없이 agent SA의 ID 토큰만으로 통과한다(iam.tf api_public).
        name  = "API_BASE_URL"
        value = var.api_oidc_audience
      }
      env {
        name  = "API_OIDC_AUDIENCE"
        value = var.api_oidc_audience
      }
      env {
        name  = "AGENT_MODEL"
        value = var.agent_model
      }
    }
  }

  lifecycle {
    # client/client_version/build_config/scaling(top-level)은 `gcloud run deploy`가
    # 배포마다 찍는 필드라 Terraform 선언과 항상 어긋난다 — 무시한다.
    ignore_changes = [
      template[0].containers[0].image,
      client,
      client_version,
      build_config,
      scaling,
    ]
  }

  depends_on = [google_project_service.apis]
}
