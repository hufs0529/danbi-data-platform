import logging

from danbi_common.logs import PiiScrubFilter
from danbi_common.pii.detect import PiiType, detect, luhn_ok, mask, rrn_confidence


def _types(text):
    return [f.type for f in detect(text)]


def test_rrn_with_valid_checksum_high_confidence():
    # 800101-1234567: 1980-01-01, 남. 검증 자리 계산 값과 맞춘 예시
    digits = "800101123456"
    weights = (2, 3, 4, 5, 6, 7, 8, 9, 2, 3, 4, 5)
    check = (11 - sum(int(d) * w for d, w in zip(digits, weights, strict=True)) % 11) % 10
    rrn = f"{digits[:6]}-{digits[6:]}{check}"
    [f] = detect(f"주민번호 {rrn} 확인")
    assert f.type == PiiType.RRN and f.confidence >= 0.9


def test_rrn_post_2020_random_suffix_still_detected():
    # 체크섬이 맞지 않아도 날짜·성별 자리가 유효하면 잡는다(신뢰도만 낮춘다)
    assert rrn_confidence("2103044000000") in (0.7, 0.95)
    assert PiiType.RRN in _types("등록번호 210304-4000000")


def test_rrn_impossible_date_rejected():
    assert rrn_confidence("801332" + "1234567") == 0.0
    assert PiiType.RRN not in _types("801332-1234567")


def test_phone_variants():
    for s in ["010-1234-5678", "01012345678", "+82 10 1234 5678", "02-123-4567", "031.123.4567"]:
        assert PiiType.PHONE in _types(f"연락처 {s} 입니다"), s


def test_card_requires_luhn():
    assert luhn_ok("4111111111111111")
    assert PiiType.CARD in _types("카드 4111-1111-1111-1111 결제")
    assert PiiType.CARD not in _types("주문번호 4111-1111-1111-1112")


def test_account_needs_bank_context():
    assert PiiType.ACCOUNT in _types("환불 계좌: 국민 123456-01-234567")
    assert PiiType.ACCOUNT not in _types("문서번호 123456-01-234567")


def test_email_and_address_and_name():
    text = "성명: 김단비, 이메일 danbi.kim@example.com, 주소 서울특별시 강남구 테헤란로 123"
    types = _types(text)
    assert {PiiType.NAME, PiiType.EMAIL, PiiType.ADDRESS} <= set(types)


def test_order_number_not_phone():
    assert _types("주문번호 20261030010123456789") == []


def test_mask_keeps_context():
    out = mask("고객 연락처 010-1234-5678 로 안내")
    assert out == "고객 연락처 [전화번호] 로 안내"


def test_log_filter_masks_message():
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "call %s", ("010-1234-5678",), None)
    PiiScrubFilter().filter(record)
    assert record.msg == "call [전화번호]"
