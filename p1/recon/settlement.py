"""PG 정산 파일 검증·파싱 (순수 함수).

판정 순서(앞에서 걸리면 멈춘다)
1. 파일명 규칙: danbipay_settle_YYYYMMDD_NN.csv → 정산일·차수(정정본은 NN 증가)
2. 내용 해시가 이미 받은 파일과 같으면 DUPLICATE (재전송) — 이름이 같아도 내용이 다르면 중복이 아니다
3. CP949 엄격 디코딩 실패 → QUARANTINED(encoding)
4. 헤더가 알려진 버전(v1, v2)과 정확히 같지 않으면 QUARANTINED(header) — 열 순서를 추측하지 않는다
5. TRAILER(건수·금액 합)가 본문과 다르면 QUARANTINED(trailer) — 잘린 파일
6. 행 단위 형식 오류가 하나라도 있으면 QUARANTINED(row) — 일부만 적재하면 대사가 거짓 일치한다
격리된 파일은 Silver 에 절대 들어가지 않는다(F-06 수용 기준).
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

NAME_RE = re.compile(r"^danbipay_settle_(\d{8})_(\d{2})\.csv$")
KST = dt.timezone(dt.timedelta(hours=9))

HEADERS = {
    1: [
        "정산일자",
        "거래일시",
        "거래ID",
        "주문번호",
        "구분",
        "결제수단",
        "카드사",
        "거래금액",
        "수수료",
        "정산금액",
    ],
    2: [
        "정산일자",
        "거래일시",
        "승인번호",
        "주문번호",
        "구분",
        "결제수단",
        "카드사",
        "거래금액",
        "수수료",
        "부가세",
        "정산금액",
    ],
}
KIND = {"매입": "CAPTURE", "취소": "CANCEL"}
METHODS = {"CARD", "EASY_PAY", "TRANSFER"}


@dataclass
class Line:
    settle_date: dt.date
    seq: int
    line_no: int
    txn_at: dt.datetime  # UTC, tz 없음
    payment_key: str
    order_no: str
    kind: str
    method: str
    issuer: str | None
    amount: Decimal
    fee: Decimal
    vat: Decimal | None  # v1 은 수수료에 부가세가 포함돼 분리할 수 없다
    net: Decimal


@dataclass
class Verdict:
    file_name: str
    sha256: str
    size_bytes: int
    status: str  # ACCEPTED | DUPLICATE | QUARANTINED
    reason: str = ""
    settle_date: dt.date | None = None
    seq: int | None = None
    header_version: int | None = None
    trailer_count: int | None = None
    trailer_amount: Decimal | None = None
    lines: list[Line] = field(default_factory=list)


def _d(s: str) -> Decimal:
    try:
        return Decimal(s.strip())
    except InvalidOperation as e:
        raise ValueError(f"금액 형식 {s!r}") from e


def inspect(file_name: str, body: bytes, known_hashes: set[str]) -> Verdict:
    sha = hashlib.sha256(body).hexdigest()
    v = Verdict(file_name, sha, len(body), "QUARANTINED")
    m = NAME_RE.match(file_name)
    if not m:
        v.reason = "name: 파일명 규칙 불일치"
        return v
    v.settle_date = dt.datetime.strptime(m.group(1), "%Y%m%d").date()
    v.seq = int(m.group(2))
    if sha in known_hashes:
        v.status, v.reason = "DUPLICATE", "같은 내용의 파일을 이미 받았다"
        return v
    try:
        text = body.decode("cp949", errors="strict")
    except UnicodeDecodeError as e:
        v.reason = f"encoding: CP949 디코딩 실패 at byte {e.start}"
        return v
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        v.reason = "empty"
        return v
    version = next((ver for ver, h in HEADERS.items() if rows[0] == h), None)
    if version is None:
        v.reason = f"header: 알 수 없는 헤더 {rows[0][:4]}..."
        return v
    v.header_version = version
    body_rows, trailer = rows[1:-1], rows[-1]
    if len(trailer) < 3 or trailer[0] != "TRAILER":
        v.reason = "trailer: 합계 행 없음(잘린 파일)"
        return v
    v.trailer_count, v.trailer_amount = int(trailer[1]), _d(trailer[2])
    lines: list[Line] = []
    for i, r in enumerate(body_rows, start=2):
        try:
            lines.append(_parse_row(r, version, v.settle_date, v.seq, i))
        except (ValueError, KeyError, IndexError) as e:
            v.reason = f"row {i}: {e}"
            return v
    if len(lines) != v.trailer_count or sum((ln.amount for ln in lines), Decimal(0)) != v.trailer_amount:
        v.reason = f"trailer: 본문 {len(lines)}건/{sum(ln.amount for ln in lines)} ≠ 합계 행 {v.trailer_count}건/{v.trailer_amount}"
        return v
    v.status, v.lines = "ACCEPTED", lines
    return v


def _parse_row(r: list[str], version: int, settle_date: dt.date, seq: int, line_no: int) -> Line:
    if len(r) != len(HEADERS[version]):
        raise ValueError(f"열 수 {len(r)}")
    if r[0] != settle_date.strftime("%Y%m%d"):
        raise ValueError(f"정산일자 {r[0]} 가 파일명과 다르다")
    txn_local = dt.datetime.strptime(r[1], "%Y%m%d%H%M%S").replace(tzinfo=KST)
    method = r[5]
    if method not in METHODS:
        raise ValueError(f"결제수단 {method}")
    amount = _d(r[7])
    if version == 1:
        fee, vat, net = _d(r[8]), None, _d(r[9])
        check = amount - fee
    else:
        fee, vat, net = _d(r[8]), _d(r[9]), _d(r[10])
        check = amount - fee - vat
    if check != net:
        raise ValueError(f"정산금액 불일치 {net} != {check}")
    return Line(settle_date, seq, line_no, txn_local.astimezone(dt.UTC).replace(tzinfo=None), r[2], r[3], KIND[r[4]],
                method, r[6] or None, amount, fee, vat, net)  # fmt: skip


def effective_seq(accepted: dict[int, str]) -> int | None:
    """같은 정산일에 받은 ACCEPTED 파일들(차수 → 해시) 중 유효본 = 가장 높은 차수(정정본)."""
    return max(accepted) if accepted else None
