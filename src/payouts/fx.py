"""schema-contract.md §4 — 승인 시점 환율 조회.

frankfurter.dev(ECB 공시 환율, 무료, 키 불필요)만 호출한다. SSRF 방지를 위해
호스트를 하드코딩하고, 통화 코드(3자리 알파벳) 외의 값은 URL에 넣지 않는다.
"""

from decimal import Decimal

import requests

FX_API_URL = "https://api.frankfurter.dev/v1/latest"


class FxRateUnavailable(Exception):
    pass


def fetch_fx_rate(from_currency: str, to_currency: str) -> Decimal:
    resp = requests.get(
        FX_API_URL,
        params={"base": from_currency, "symbols": to_currency},
        timeout=5,
    )
    resp.raise_for_status()
    rate = resp.json().get("rates", {}).get(to_currency)
    if rate is None:
        raise FxRateUnavailable(f"{from_currency}/{to_currency}")
    return Decimal(str(rate))
