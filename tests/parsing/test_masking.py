"""schema-contract.md §2 — Firestore에 들어가는 값은 전부 마스킹 후다.

이 스위트의 절반은 "지우는지"가 아니라 **"안 지우는지"**를 본다. merchant_name은
결정론적 매칭(§6 가맹점명 축)이 쓰는 필드라, 과하게 마스킹하면 매칭이 조용히
전부 실패한다. 지우는 것보다 남기는 걸 더 촘촘히 테스트하는 이유다.
"""

import glob
import json

import pytest

from src.parsing.masking import hash_receipt_serial_number, mask_pii


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("스타벅스 강남점 02-1234-5678", "스타벅스 강남점 [PHONE]"),
        ("문의 help@store.example.com", "문의 [EMAIL]"),
        ("카드 1234-5678-9012-3456 승인", "카드 [CARD] 승인"),
        ("카드 1234567890123456 승인", "카드 [CARD] 승인"),
        ("사업자등록번호 123-45-67890", "사업자등록번호 [BIZNO]"),
        ("주민번호 900101-1234567", "주민번호 [RRN]"),
        ("연락처 010-1111-2222", "연락처 [PHONE]"),
    ],
)
def test_masks_pii_patterns(raw, expected):
    assert mask_pii(raw) == expected


@pytest.mark.parametrize(
    "merchant",
    [
        "다이소 강남점",
        "Notion Labs Inc",
        "카카오모빌리티",
        "무명상점",
        "스타벅스 1호점",
        "GS25 역삼2호점",
    ],
)
def test_leaves_merchant_names_intact(merchant):
    """상호에 붙은 숫자(1호점, GS25, 역삼2호점)를 전화번호·카드번호로 오인하면 안 된다."""
    assert mask_pii(merchant) == merchant


def test_amount_like_digits_are_not_masked():
    """금액 자릿수가 카드번호 패턴에 걸리면 매칭과 감사 로그가 동시에 망가진다."""
    assert mask_pii("합계 45,000원") == "합계 45,000원"
    assert mask_pii("2026-08-05 결제 99000") == "2026-08-05 결제 99000"


def test_none_passes_through():
    assert mask_pii(None) is None


def test_masks_injection_fixture_raw_text():
    """fixture 06의 인젝션 원문. 마스킹은 인젝션을 막는 장치가 아니지만(그건 §5
    1단계 게이트다), 이 텍스트가 audit_logs.reason으로 흘러도 함수가 죽지 않고
    지시문을 그대로 통과시킨다는 걸 못 박는다 — 마스킹이 인젝션 방어인 척하면
    진짜 게이트를 안 만들게 된다."""
    with open("tests/fixtures/06_prompt_injection.json", encoding="utf-8") as f:
        sample = json.load(f)["_fixture_note_raw_text_sample"]

    masked = mask_pii(sample)
    assert masked is not None
    assert "SYSTEM:" in masked  # 마스커는 지시문을 제거하지 않는다. 그건 게이트의 몫이다.


def test_mask_pii_collapses_long_numeric_serials_to_the_same_literal():
    """이 회귀 테스트가 지키는 버그: receipt_serial_number(카드 승인번호·거래고유
    번호)에 mask_pii를 그대로 썼을 때, 서로 다른 두 번호가 카드번호 패턴
    (13~19자리)에 걸려 똑같은 "[CARD]"로 뭉개졌다 — 이게 find_exact_duplicate_receipts의
    유일성 전제를 깨서 무관한 영수증들을 완전일치 중복으로 오판하게 만든 원인이다.
    hash_receipt_serial_number를 대신 쓰는 이유가 바로 이 결과다."""
    assert mask_pii("2026082101001234") == mask_pii("2026082201009999") == "[CARD]"


@pytest.mark.parametrize(
    "a, b",
    [
        ("2026082101001234", "2026082201009999"),
        ("1234567890123", "1234567890124"),
    ],
)
def test_hash_receipt_serial_number_does_not_collide_for_different_inputs(a, b):
    assert hash_receipt_serial_number(a) != hash_receipt_serial_number(b)


def test_hash_receipt_serial_number_is_deterministic():
    assert hash_receipt_serial_number("A1234") == hash_receipt_serial_number("A1234")


def test_hash_receipt_serial_number_none_and_empty_pass_through_as_none():
    assert hash_receipt_serial_number(None) is None
    assert hash_receipt_serial_number("") is None


def test_all_fixture_merchant_names_survive_masking():
    """fixture 9종의 merchant_name 전부가 마스킹으로 훼손되지 않는지 본다.
    이게 깨지면 결정론적 매칭이 실 데이터에서 전부 어긋난다."""
    names = []
    for path in sorted(glob.glob("tests/fixtures/*.json")):
        with open(path, encoding="utf-8") as f:
            for receipt in json.load(f).get("receipts", []):
                if receipt.get("merchant_name"):
                    names.append(receipt["merchant_name"])

    assert names, "fixture에서 merchant_name을 하나도 못 읽었다"
    for name in names:
        assert mask_pii(name) == name, f"마스킹이 상호명을 훼손했다: {name}"
