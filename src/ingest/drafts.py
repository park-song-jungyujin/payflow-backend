"""schema-contract.md §9 — 청구자 에이전트 draft payload 판정.

payload는 청구자 에이전트가 LLM 출력을 담아 보낸 **비신뢰 입력**이다. 필드가
빠지거나 타입이 어긋나거나, 계약에 없는 필드가 붙어 올 수 있다. 여기서 걸러
트랜잭션까지 안 내려보내는 게 이 모듈의 목적이므로, 순수 함수로 두고
Firestore·네트워크에는 닿지 않는다.

`needs_requery`는 이후 분기(재요청 여부)의 근거이므로 없거나 타입이 틀리면
추측하지 않고 거부한다. 반대로 계약에 없는 필드는 §9가 최상위 4키만 정의하고
있으므로 조용히 무시한다 — `extra="forbid"`를 쓰면 에이전트가 필드를 하나
더 붙였다는 이유만으로 반영 자체가 막혀버린다.
"""

from pydantic import BaseModel, ConfigDict


class InvalidDraftPayload(RuntimeError):
    """payload가 §9 계약과 형식적으로 맞지 않을 때."""


class DraftVerdict(BaseModel):
    model_config = ConfigDict(extra="ignore")

    needs_requery: bool
    is_business: bool | None = None
    requery_message: str | None = None
    reason: str | None = None


def parse_claimant_payload(payload: dict) -> DraftVerdict:
    """청구자 draft의 payload를 판정 모델로 변환한다.

    `needs_requery`가 없거나 `bool`이 아니면 판정의 근거가 없으므로
    `InvalidDraftPayload`를 던진다. 계약에 없는 필드는 조용히 버려진다.

    payload 자체가 dict가 아니면(`str`·`list`·`None` 등) `.get()`이 없어 여기서
    `AttributeError`로 새 나간다 — 호출부(라우트)가 `InvalidDraftPayload`만 잡으므로
    형식이 아예 다른 payload는 먼저 여기서 걸러야 한다.
    """
    if not isinstance(payload, dict):
        raise InvalidDraftPayload(f"payload must be a dict, got {type(payload).__name__}")

    if not isinstance(payload.get("needs_requery"), bool):
        raise InvalidDraftPayload("needs_requery must be a bool")

    is_business = payload.get("is_business")
    if is_business is not None and not isinstance(is_business, bool):
        raise InvalidDraftPayload("is_business must be a bool or None")

    try:
        return DraftVerdict(**payload)
    except Exception as exc:
        raise InvalidDraftPayload(str(exc)) from exc
