# 데모 fixture 8종

`docs/rules/schema-contract.md` §12에 정의된 시나리오 8개. 스텁 엔드포인트가 그대로
반환하고, 최종 데모도 이 데이터를 쓴다. **추가만 하고 기존 fixture를 수정하지 않는다.**

| 파일 | # | 시나리오 | 시연 |
|---|---|---|---|
| `01_golden_path_fx.json` | 1 | 정상 매칭, 항목 하나 USD | B |
| `02_parse_failure_requery.json` | 2 | 파싱 실패 → 재요청 | A |
| `03_duplicate_claim.json` | 3 | 중복 청구 의심 | B |
| `04_low_confidence_unclassified.json` | 4 | confidence 낮음 → UNCLASSIFIED | B |
| `05_reminder_expired.json` | 5 | 미청구 → 재촉 → 만료 | A |
| `06_prompt_injection.json` | 6 | 프롬프트 인젝션 시도 | A |
| `07_cap_exceeded_and_no_token.json` | 7 | 한도 캡 초과 + 토큰 없는 `/payouts` → 403 | C |
| `08_payout_failed_unclaimed.json` | 8 | PayPal FAILED + UNCLAIMED 혼재 | C |

## 표기 규칙

- Firestore `Timestamp` 필드는 ISO-8601 UTC 문자열(`"...Z"`)로 표기한다. 실제 저장 시
  코드가 `Timestamp`로 변환한다.
- `receipts.transaction_date`만 `YYYY-MM-DD` 순수 날짜 문자열이다 (스키마 §1과 동일).
- ID는 스키마 §3 형식을 따르되 사람이 읽기 쉽게 시나리오 번호를 심었다. 실제 ULID처럼
  진짜 정렬 속성을 갖진 않는다 — fixture 전용이다.
- `_fixture_note`로 시작하는 키는 Firestore 문서에 없는 fixture 전용 주석이다. 스텁이
  그대로 반환해도 무해하지만, 실제 스키마 필드가 아니므로 Pydantic 모델에는 없다.
- 한도 캡(`MAX_AMOUNT_*_MINOR`) 등 환경변수 의존 값은 `.env`의 실제 값이 아니라 시연에
  맞춘 예시값이다. 스텁이 이 fixture를 반환할 때는 해당 `.env` 캡을 fixture 총액보다
  낮게 맞춰야 7번 시나리오가 실제로 403을 낸다.
