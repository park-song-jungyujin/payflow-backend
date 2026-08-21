"""schema-contract.md §9 — 청구자 에이전트 payload 판정.

payload는 **비신뢰 입력이다.** 에이전트가 LLM 출력을 담아 보내므로 필드가
빠지거나 타입이 어긋날 수 있다. 여기서 걸러 트랜잭션까지 안 내려보낸다.
"""

import pytest

from src.ingest.drafts import InvalidDraftPayload, parse_claimant_payload


def test_parses_requery_verdict():
    v = parse_claimant_payload({
        "needs_requery": True,
        "requery_message": "사진이 흐릿해요. 다시 올려주시겠어요?",
        "is_business": None,
        "reason": "amount_parsed=false",
    })
    assert v.needs_requery is True
    assert v.requery_message.startswith("사진이")
    assert v.is_business is None


def test_parses_pass_verdict():
    v = parse_claimant_payload({
        "needs_requery": False, "requery_message": None,
        "is_business": True, "reason": "영수증과 청구 금액 일치",
    })
    assert v.needs_requery is False
    assert v.is_business is True


@pytest.mark.parametrize("payload", [
    {},                                    # needs_requery 없음
    {"needs_requery": "yes"},              # bool 아님
    {"needs_requery": None},
])
def test_rejects_missing_or_wrong_needs_requery(payload):
    """needs_requery는 분기의 근거다. 없거나 타입이 틀리면 추측하지 않는다."""
    with pytest.raises(InvalidDraftPayload):
        parse_claimant_payload(payload)


def test_rejects_non_bool_is_business():
    with pytest.raises(InvalidDraftPayload):
        parse_claimant_payload({"needs_requery": False, "is_business": "true"})


def test_ignores_unknown_fields():
    """에이전트가 없는 필드를 지어내도 조용히 무시한다 — 계약은 최상위 4키뿐이다(§9)."""
    v = parse_claimant_payload({
        "needs_requery": False, "is_business": True,
        "approve_immediately": True, "amount_minor": 999999,
    })
    assert v.needs_requery is False
    assert not hasattr(v, "amount_minor")


def test_requery_without_message_is_allowed():
    """문안이 비어도 판정은 유효하다. DM 문안은 재촉 루프가 채운다."""
    v = parse_claimant_payload({"needs_requery": True})
    assert v.needs_requery is True
    assert v.requery_message is None
