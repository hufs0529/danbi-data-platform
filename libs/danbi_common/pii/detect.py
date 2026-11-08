"""한국어 텍스트의 개인정보 탐지와 마스킹.

규칙 + 검증(체크섬·날짜·문맥)으로 오탐을 줄인다. 이름처럼 규칙으로 못 잡는 것은 문맥 키워드("성명:")가
있을 때만 잡는다. 관리형 PII 서비스 대신 직접 구현한 이유는 ADR AWS-11 참고.

주의: 2020년 10월 이후 발급된 주민등록번호는 뒤 6자리가 임의 번호라 검증 자리 규칙이 맞지 않는다.
그래서 체크섬 실패를 '아님'으로 보지 않고 신뢰도만 낮춘다(과소 마스킹보다 과대 마스킹이 안전).
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from enum import StrEnum


class PiiType(StrEnum):
    RRN = "주민등록번호"
    PHONE = "전화번호"
    EMAIL = "이메일"
    CARD = "카드번호"
    ACCOUNT = "계좌번호"
    PASSPORT = "여권번호"
    DRIVER_LICENSE = "운전면허번호"
    ADDRESS = "주소"
    NAME = "이름"


@dataclass(frozen=True)
class Finding:
    type: PiiType
    start: int
    end: int
    value: str
    confidence: float  # 0~1


# 숫자 앞뒤가 다른 숫자에 붙어 있지 않게(긴 주문번호의 일부를 잡지 않도록) 경계를 건다
_B = r"(?<![\d-])"
_E = r"(?![\d-])"

RRN_RE = re.compile(_B + r"(\d{2})(\d{2})(\d{2})\s?-?\s?([1-8])(\d{6})" + _E)
PHONE_RE = re.compile(
    _B
    + r"(?:\+?82[\s-]?)?(?:0?1[016789][\s.-]?\d{3,4}[\s.-]?\d{4}|0?(?:2|[3-6][1-5]|70)[\s.-]?\d{3,4}[\s.-]?\d{4})"
    + _E
)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
CARD_RE = re.compile(_B + r"(?:\d[ -]?){12,18}\d" + _E)
ACCOUNT_RE = re.compile(_B + r"\d{2,6}-\d{2,6}-\d{2,7}(?:-\d{1,3})?" + _E)
PASSPORT_RE = re.compile(r"(?<![A-Za-z0-9])(?:[MSRODG]\d{8}|[MSRODG]\d{3}[A-Z]\d{4})(?![A-Za-z0-9])")
LICENSE_RE = re.compile(_B + r"\d{2}-\d{2}-\d{6}-\d{2}" + _E)
ADDRESS_RE = re.compile(
    r"[가-힣]{2,}(?:특별시|광역시|특별자치시|특별자치도|도|시)\s+[가-힣]{1,}(?:시|군|구)"
    r"(?:\s+[가-힣]{1,}(?:구|읍|면))?\s+[가-힣0-9]+(?:로|길)\s*\d+(?:-\d+)?(?:\s*\d+동)?(?:\s*\d+호)?"
)
NAME_CTX_RE = re.compile(r"(?:성명|이름|고객명|수취인|예금주|담당자)\s*[:：]?\s*([가-힣]{2,4})(?![가-힣])")

ACCOUNT_CONTEXT = re.compile(
    r"계좌|은행|입금|송금|이체|예금주|국민|신한|우리|하나|농협|기업|카카오뱅크|토스뱅크|케이뱅크|우체국|새마을"
)

_RRN_WEIGHTS = (2, 3, 4, 5, 6, 7, 8, 9, 2, 3, 4, 5)


def _rrn_century(gender_digit: int) -> int:
    return {1: 1900, 2: 1900, 5: 1900, 6: 1900, 3: 2000, 4: 2000, 7: 2000, 8: 2000}[gender_digit]


def rrn_confidence(digits: str) -> float:
    """13자리 주민등록번호 후보의 신뢰도. 0 이면 형식상 불가능."""
    if len(digits) != 13 or not digits.isdigit():
        return 0.0
    yy, mm, dd, g = int(digits[0:2]), int(digits[2:4]), int(digits[4:6]), int(digits[6])
    if g not in (1, 2, 3, 4, 5, 6, 7, 8):
        return 0.0
    try:
        born = dt.date(_rrn_century(g) + yy, mm, dd)
    except ValueError:
        return 0.0
    if born > dt.date.today():
        return 0.0
    check = (11 - sum(int(d) * w for d, w in zip(digits[:12], _RRN_WEIGHTS, strict=True)) % 11) % 10
    return 0.95 if check == int(digits[12]) else 0.7


def luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def detect(text: str) -> list[Finding]:
    found: list[Finding] = []

    for m in RRN_RE.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        conf = rrn_confidence(digits)
        if conf > 0:
            found.append(Finding(PiiType.RRN, m.start(), m.end(), m.group(0), conf))

    for m in CARD_RE.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and luhn_ok(digits) and not set(digits) <= {"0"}:
            found.append(Finding(PiiType.CARD, m.start(), m.end(), m.group(0), 0.9))

    for m in PHONE_RE.finditer(text):
        found.append(Finding(PiiType.PHONE, m.start(), m.end(), m.group(0), 0.85))

    for m in EMAIL_RE.finditer(text):
        found.append(Finding(PiiType.EMAIL, m.start(), m.end(), m.group(0), 0.95))

    for m in LICENSE_RE.finditer(text):
        found.append(Finding(PiiType.DRIVER_LICENSE, m.start(), m.end(), m.group(0), 0.8))

    for m in ACCOUNT_RE.finditer(text):
        window = text[max(0, m.start() - 20) : m.start()]
        if ACCOUNT_CONTEXT.search(window):
            found.append(Finding(PiiType.ACCOUNT, m.start(), m.end(), m.group(0), 0.8))

    for m in PASSPORT_RE.finditer(text):
        found.append(Finding(PiiType.PASSPORT, m.start(), m.end(), m.group(0), 0.7))

    for m in ADDRESS_RE.finditer(text):
        found.append(Finding(PiiType.ADDRESS, m.start(), m.end(), m.group(0), 0.75))

    for m in NAME_CTX_RE.finditer(text):
        found.append(Finding(PiiType.NAME, m.start(1), m.end(1), m.group(1), 0.6))

    return _resolve_overlaps(found)


# 겹칠 때 우선순위: 체크섬으로 검증되는 것이 먼저
_PRIORITY = {
    PiiType.RRN: 0,
    PiiType.CARD: 1,
    PiiType.DRIVER_LICENSE: 2,
    PiiType.ACCOUNT: 3,
    PiiType.EMAIL: 4,
    PiiType.PHONE: 5,
    PiiType.PASSPORT: 6,
    PiiType.ADDRESS: 7,
    PiiType.NAME: 8,
}


def _resolve_overlaps(findings: list[Finding]) -> list[Finding]:
    ordered = sorted(findings, key=lambda f: (_PRIORITY[f.type], -(f.end - f.start), f.start))
    kept: list[Finding] = []
    for f in ordered:
        if all(f.end <= k.start or f.start >= k.end for k in kept):
            kept.append(f)
    return sorted(kept, key=lambda f: f.start)


def mask(text: str, findings: list[Finding] | None = None) -> str:
    """탐지된 구간을 [유형] 으로 바꾼다. 문맥은 남기고 값만 지운다."""
    findings = detect(text) if findings is None else findings
    out: list[str] = []
    pos = 0
    for f in sorted(findings, key=lambda f: f.start):
        out.append(text[pos : f.start])
        out.append(f"[{f.type.value}]")
        pos = f.end
    out.append(text[pos:])
    return "".join(out)
