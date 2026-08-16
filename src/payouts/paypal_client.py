"""schema-contract.md §8 — PayPal Payouts API 직접 호출. MCP Server를 끼면
멱등성·게이트·캡의 통제점이 흐려진다.

절대 규칙: access token과 client secret은 반환값 밖으로 새어나가지 않는다.
audit 로그에는 PayPal이 돌려준 batch id만 남긴다.
"""

import os

import requests


def _base_url() -> str:
    if os.environ.get("PAYPAL_ENV") == "live":
        return "https://api-m.paypal.com"
    return "https://api-m.sandbox.paypal.com"


def get_access_token() -> str:
    client_id = os.environ["PAYPAL_CLIENT_ID"]
    client_secret = os.environ["PAYPAL_CLIENT_SECRET"]
    resp = requests.post(
        f"{_base_url()}/v1/oauth2/token",
        auth=(client_id, client_secret),
        data={"grant_type": "client_credentials"},
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def create_payout(sender_batch_id: str, items: list[dict]) -> dict:
    """items: [{recipient_type, amount: {value, currency}, receiver, sender_item_id}, ...].

    `PayPal-Request-Id`와 `sender_batch_id`는 동일 값을 쓴다 — schema-contract.md §3.
    """
    token = get_access_token()
    body = {
        "sender_batch_header": {
            "sender_batch_id": sender_batch_id,
            "email_subject": "Payflow 정산 송금",
        },
        "items": items,
    }
    resp = requests.post(
        f"{_base_url()}/v1/payments/payouts",
        headers={
            "Authorization": f"Bearer {token}",
            "PayPal-Request-Id": sender_batch_id,
        },
        json=body,
    )
    resp.raise_for_status()
    return resp.json()


def get_payout_batch(payout_batch_id: str) -> dict:
    """생성 응답에는 item별 payout_item_id가 없어, 조회로 한 번 더 받는다."""
    token = get_access_token()
    resp = requests.get(
        f"{_base_url()}/v1/payments/payouts/{payout_batch_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    resp.raise_for_status()
    return resp.json()
