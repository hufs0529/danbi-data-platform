"""가상 PG(단비페이 PG사) 일별 정산 파일 생성.

국내 PG 정산 파일에서 실제로 겪는 문제를 그대로 넣는다.
- CP949 인코딩, 마지막 줄 합계 행(TRAILER), 23:30 KST 매입 마감(이후 거래는 다음 날 파일)
- 헤더 버전 변경(v2: '거래ID' → '승인번호', 부가세 컬럼 분리)
- 같은 파일 재전송(중복), 정정본 재전송(_02), 인코딩 깨짐, PG 쪽 누락·금액 차이·내부에 없는 거래
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import random
from dataclasses import dataclass, replace
from decimal import ROUND_DOWN, Decimal

KST = dt.timezone(dt.timedelta(hours=9))
CUTOFF = dt.time(23, 30)
FEE_RATE = {"CARD": Decimal("0.020"), "EASY_PAY": Decimal("0.025"), "TRANSFER": Decimal("0.015")}

HEADER_V1 = [
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
]
HEADER_V2 = [
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
]


@dataclass(frozen=True)
class Txn:
    payment_key: str
    order_no: str
    kind: str  # CAPTURE | CANCEL
    method: str
    issuer: str | None
    amount: Decimal  # 양수. 취소도 양수로 받고 파일에서 음수로 쓴다
    occurred_at: dt.datetime  # UTC


@dataclass
class SettlementFaults:
    missing_ratio: float = 0.0  # PG 파일에서 빠진 거래 → internal_only
    amount_skew_ratio: float = 0.0  # 금액 차이 → amount_mismatch
    extra_count: int = 0  # 내부에 없는 거래 → pg_only
    header_version: int = 1
    broken_encoding: bool = False  # UTF-8 로 써 놓고 CP949 라고 보냄
    duplicate: bool = False  # 같은 파일을 두 번 보냄
    correction: bool = False  # 정정본(_02): 금액 차이를 고친 전체 재전송


def settle_date(occurred_at_utc: dt.datetime) -> dt.date:
    local = occurred_at_utc.astimezone(KST)
    return local.date() + dt.timedelta(days=1) if local.time() >= CUTOFF else local.date()


def window_utc(d: dt.date) -> tuple[dt.datetime, dt.datetime]:
    """정산일 d 에 들어가는 거래의 UTC 구간 [전날 23:30 KST, 당일 23:30 KST)."""
    end = dt.datetime.combine(d, CUTOFF, KST)
    return (end - dt.timedelta(days=1)).astimezone(dt.UTC), end.astimezone(dt.UTC)


def fee_parts(amount: Decimal, method: str) -> tuple[Decimal, Decimal]:
    fee = (abs(amount) * FEE_RATE[method]).quantize(Decimal(1), ROUND_DOWN)
    vat = (fee * Decimal("0.1")).quantize(Decimal(1), ROUND_DOWN)
    sign = -1 if amount < 0 else 1
    return fee * sign, vat * sign


def _rows(d: dt.date, txns: list[Txn], header_version: int) -> list[list[str]]:
    rows = []
    for t in sorted(txns, key=lambda t: t.occurred_at):
        amount = t.amount if t.kind == "CAPTURE" else -t.amount
        fee, vat = fee_parts(amount, t.method)
        base = [d.strftime("%Y%m%d"), t.occurred_at.astimezone(KST).strftime("%Y%m%d%H%M%S"), t.payment_key,
                t.order_no, "매입" if t.kind == "CAPTURE" else "취소", t.method, t.issuer or ""]  # fmt: skip
        if header_version == 1:
            rows.append(base + [str(amount), str(fee + vat), str(amount - fee - vat)])
        else:
            rows.append(base + [str(amount), str(fee), str(vat), str(amount - fee - vat)])
    return rows


def render(d: dt.date, txns: list[Txn], header_version: int = 1, encoding: str = "cp949") -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(HEADER_V1 if header_version == 1 else HEADER_V2)
    rows = _rows(d, txns, header_version)
    w.writerows(rows)
    total = sum(Decimal(r[7]) for r in rows)
    w.writerow(["TRAILER", str(len(rows)), str(total)])
    return buf.getvalue().encode(encoding)


def file_name(d: dt.date, seq: int) -> str:
    return f"danbipay_settle_{d:%Y%m%d}_{seq:02d}.csv"


def build_files(
    d: dt.date, internal: list[Txn], faults: SettlementFaults | None = None, seed: int = 7
) -> list[tuple[str, bytes]]:
    """PG 가 보낸다고 가정한 파일들(전송 순서대로)."""
    f = faults or SettlementFaults()
    rng = random.Random(seed)
    lo, hi = window_utc(d)
    in_window = [t for t in internal if lo <= t.occurred_at < hi]

    pg_view: list[Txn] = []
    skewed: dict[str, Txn] = {}
    for t in in_window:
        if rng.random() < f.missing_ratio:
            continue
        if rng.random() < f.amount_skew_ratio:
            bad = replace(t, amount=t.amount + Decimal(rng.choice([-1000, -100, 100, 1000])))
            skewed[t.payment_key] = t
            pg_view.append(bad)
        else:
            pg_view.append(t)
    for i in range(f.extra_count):
        pg_view.append(Txn(f"pg_extra{d:%Y%m%d}{i:04d}", f"OEXT{i:06d}", "CAPTURE", "CARD", "BC",
                           Decimal(rng.randrange(1000, 50000, 100)), lo + dt.timedelta(hours=rng.randint(1, 20))))  # fmt: skip

    enc = "utf-8" if f.broken_encoding else "cp949"
    files = [(file_name(d, 1), render(d, pg_view, f.header_version, enc))]
    if f.duplicate:
        files.append((file_name(d, 1), files[0][1]))
    if f.correction:
        fixed = [skewed.get(t.payment_key, t) for t in pg_view]
        files.append((file_name(d, 2), render(d, fixed, f.header_version, "cp949")))
    return files


# ---------------------------------------------------------------------------
# 결제 DB 에서 정산 대상 거래 읽기 (generator settle 명령)
# ---------------------------------------------------------------------------
SETTLEMENT_SQL = """
SELECT p.payment_key, p.order_no, h.to_status, p.method, p.card_issuer, h.amount, h.occurred_at
FROM payment_status_history h
JOIN payments p ON p.payment_id = h.payment_id
WHERE h.to_status IN ('CAPTURED', 'PARTIAL_CANCELED', 'CANCELED')
  AND h.occurred_at >= %s AND h.occurred_at < %s
"""


def txns_from_rows(rows) -> list[Txn]:
    out = []
    for key, order_no, to_status, method, issuer, amount, occurred_at in rows:
        out.append(Txn(key, order_no, "CAPTURE" if to_status == "CAPTURED" else "CANCEL", method, issuer,
                       Decimal(amount), occurred_at.astimezone(dt.UTC)))  # fmt: skip
    return out
