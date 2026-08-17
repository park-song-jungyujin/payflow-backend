# payflow-backend (`api`)

FastAPI / Python. Cloud Run 배포. 시크릿: PayPal, Slack.

**이 레포가 돈이 나가는 유일한 지점이다.** 여기 변경은 리뷰 게이트가 걸린다.

## 이 레포의 책임

- 유일하게 PayPal을 호출하는 서비스
- Slack webhook 수신 및 서명 검증
- 승인 토큰 발급 / 검증
- 영수증 이미지 → 구조화 JSON (Gemini structured output **단발 호출**, ADK 아님)
- 정산 실행 시 영수증 이미지 ↔ 파싱 결과 검증 (파싱과 별개인 Gemini 단발 호출, ADK 아님)
- 결정론적 매칭 (금액 · 날짜 윈도우 · 가맹점명)
- Firestore 쓰기의 단일 창구
- 스키마 단일 소스 — `src/schemas/*.py`의 Pydantic 모델
- Terraform은 이 레포 `infra/`에 둔다. 별도 레포로 빼지 않는다

## 브랜치 게이트

아래는 `main` 직접 푸시 금지. 브랜치를 파고 다른 사람이 한 번 본다.

- payout 관련 전부
- 승인 토큰 발급 / 검증
- 상태 전이 CAS 로직

## 절대 어기지 않는 것

- 승인 응답에서 PayPal을 **동기 호출하지 않는다.** `executing` 마킹 후 Cloud Tasks로 위임
- `sender_batch_id`에 `settlement_run_id`를 그대로 넣는다. 새 UUID 생성 금지
- `draft → approved` 전이는 Firestore 트랜잭션 안에서 CAS로 처리한다
- 금액은 정수 minor unit. `float` 금지
- PII 마스킹은 Firestore 쓰기 **전에**. 원본은 GCS에만

스키마를 바꾸면 커밋 메시지에 `schema:` 접두사를 붙이고 영향받는 레포를 본문에 적는다.

## 공통 규칙

@docs/CLAUDE.md
