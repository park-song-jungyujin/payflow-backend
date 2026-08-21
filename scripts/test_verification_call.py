"""Vertex AI Gemini 검증 호출 스모크 테스트 — 실제 GCS 이미지 + 실제 ADC로 1회 호출.

schema-contract.md §2 "검증" 구현(src/settlements/verification.py)은 이 샌드박스
에서 확인할 수 없었다 — GCP 자격증명이 없다. GCP 접근 권한이 있는 사람이 먼저
이 스크립트로 한 번 돌려보고, 응답 형태가 VerificationSignals와 맞는지 확인한다.

PayPal 쪽의 scripts/test_payout_idempotency.py와 같은 자리다 — 실서비스 코드가
아니라 1회성 검증 스크립트.

사용법:
    cd backend && python scripts/test_verification_call.py gs://버킷/경로.jpg
"""

import os
import sys

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.settlements.verification import call_verification_model, verify_passed  # noqa: E402


def main() -> None:
    if len(sys.argv) != 2:
        print("사용법: python scripts/test_verification_call.py gs://버킷/경로.jpg")
        sys.exit(1)

    image_gcs_uri = sys.argv[1]
    receipt = {
        "image_gcs_uri": image_gcs_uri,
        # 의도적으로 사람이 봤을 때 이미지와 다를 값을 넣는다 — amount_matches가
        # False로 나오면 verify_passed도 False가 되는지 함께 확인한다.
        "parsed_amount_minor": 1,
        "merchant_name": "존재하지-않을-가맹점-이름-xyz",
        "transaction_date": "1999-01-01",
    }

    print(f"GCP_PROJECT={os.environ.get('GCP_PROJECT')}")
    print(f"VERTEX_LOCATION={os.environ.get('VERTEX_LOCATION')}")
    print(f"GEMINI_MODEL_ID={os.environ.get('GEMINI_MODEL_ID')}")
    print(f"이미지: {image_gcs_uri}\n")

    signals = call_verification_model(receipt)
    print("VerificationSignals:", signals.model_dump())

    passed = verify_passed(receipt, signals)
    print(f"\nverify_passed: {passed}")
    if passed:
        print("경고: 일부러 틀린 값을 넣었는데 통과했다 — 프롬프트나 스키마를 다시 본다.")
    else:
        print("의도대로 탈락 — 호출·판정 경로가 살아있다.")


if __name__ == "__main__":
    main()
