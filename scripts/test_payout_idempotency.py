"""PayPal Sandbox payout 성공 + 동일 sender_batch_id 재시도 무해성 확인.

schema-contract.md §3, §8 / money-safety.md 멱등성 규칙을 따른 1회성 검증 스크립트다.
실서비스 코드가 아니라 Phase 1 리스크 관통용 — api/src/payouts/는 이후 정식으로 만든다.

사용법:
    cd backend && python scripts/test_payout_idempotency.py
"""

import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

PAYPAL_ENV = os.environ.get("PAYPAL_ENV", "sandbox")
BASE_URL = (
    "https://api-m.sandbox.paypal.com"
    if PAYPAL_ENV == "sandbox"
    else "https://api-m.paypal.com"
)


def get_access_token() -> str:
    client_id = os.environ["PAYPAL_CLIENT_ID"]
    client_secret = os.environ["PAYPAL_CLIENT_SECRET"]
    resp = requests.post(
        f"{BASE_URL}/v1/oauth2/token",
        auth=(client_id, client_secret),
        data={"grant_type": "client_credentials"},
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def send_payout(token: str, sender_batch_id: str, receiver_email: str) -> dict:
    """동일 sender_batch_id로 여러 번 불러도 안전해야 한다 (money-safety.md 멱등성)."""
    body = {
        "sender_batch_header": {
            "sender_batch_id": sender_batch_id,
            "email_subject": "Payflow 테스트 송금",
        },
        "items": [
            {
                "recipient_type": "EMAIL",
                "amount": {"value": "1.00", "currency": "USD"},
                "receiver": receiver_email,
                "sender_item_id": f"{sender_batch_id}:test-recipient",
            }
        ],
    }
    resp = requests.post(
        f"{BASE_URL}/v1/payments/payouts",
        headers={
            "Authorization": f"Bearer {token}",
            "PayPal-Request-Id": sender_batch_id,
        },
        json=body,
    )
    return {"status_code": resp.status_code, "body": resp.json()}


def main():
    if len(sys.argv) < 2:
        print("사용법: python scripts/test_payout_idempotency.py <sandbox 수취인 이메일>")
        sys.exit(1)
    receiver_email = sys.argv[1]

    token = get_access_token()
    print("access token 발급 성공")

    sender_batch_id = sys.argv[2] if len(sys.argv) > 2 else "test_run_20260816_idempotency"

    print("\n=== 1차 호출 ===")
    r1 = send_payout(token, sender_batch_id, receiver_email)
    print(r1["status_code"], r1["body"].get("batch_header", {}).get("payout_batch_id"))

    print("\n=== 2차 호출 (동일 sender_batch_id, 재시도 시뮬레이션) ===")
    r2 = send_payout(token, sender_batch_id, receiver_email)
    print(r2["status_code"], r2["body"].get("batch_header", {}).get("payout_batch_id"))
    if r2["status_code"] != 201:
        print("2차 응답 본문:", r2["body"])

    same_batch = r1["body"].get("batch_header", {}).get("payout_batch_id") == r2["body"].get(
        "batch_header", {}
    ).get("payout_batch_id")
    print(f"\n동일 배치로 처리됨(멱등성 확인): {same_batch}")


if __name__ == "__main__":
    main()
