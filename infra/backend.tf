# terraform state를 GCS로 이전 (2026-08-22). 이전에는 로컬 머신에만 있어서
# 다른 환경에서 apply하면 충돌했다 (versions.tf 옛 경고 참조). 이제 이 버킷이
# state의 단일 소스이고, GCS의 상태 락으로 동시 apply 충돌도 막는다.
terraform {
  backend "gcs" {
    bucket = "payflow-hackathon-2026-tfstate"
    prefix = "terraform/state"
  }
}
