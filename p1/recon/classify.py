"""내부 결제 vs PG 정산 대사: 4분류 + 원인 코드 (순수 함수).

매칭 키는 (payment_key, 구분). 한 결제가 같은 날 여러 번 부분취소될 수 있어 금액을 합산해 비교한다.

| 분류 | 원인 코드 | 설명됨? |
| --- | --- | --- |
| MATCHED | - | - |
| AMOUNT_MISMATCH | AMOUNT_DIFF | 아니오 |
| INTERNAL_ONLY | TIMING_NEXT_DAY(다음 날 PG 파일에 있음) | 예 |
| INTERNAL_ONLY | PG_PENDING(다음 날 파일 아직 없음) | 보류 |
| INTERNAL_ONLY | PG_MISSING | 아니오 |
| PG_ONLY | TIMING_PREV_DAY(내부는 전날 정산일로 분류) | 예 |
| PG_ONLY | INTERNAL_STATUS_LAG(결제는 있는데 내부 상태 미반영) | 아니오 |
| PG_ONLY | UNKNOWN_TXN(내부에 없는 거래) | 아니오(위험) |
수수료가 기대값과 다르면 fee_mismatch 플래그를 따로 단다(금액 대사와 별개).
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

FEE_RATE = {"CARD": Decimal("0.020"), "EASY_PAY": Decimal("0.025"), "TRANSFER": Decimal("0.015")}
EXPLAINED = {"TIMING_NEXT_DAY", "TIMING_PREV_DAY"}
PENDING = {"PG_PENDING"}


@dataclass(frozen=True)
class Result:
    settle_date: dt.date
    payment_key: str
    kind: str
    internal_amount: Decimal | None
    pg_amount: Decimal | None
    diff: Decimal
    classification: str
    cause_code: str | None
    explained: bool
    fee_mismatch: bool


def expected_fee_incl_vat(amount: Decimal, method: str) -> Decimal:
    fee = (abs(amount) * FEE_RATE[method]).quantize(Decimal(1), ROUND_DOWN)
    vat = (fee * Decimal("0.1")).quantize(Decimal(1), ROUND_DOWN)
    return (fee + vat) * (1 if amount >= 0 else -1)


def _sum(rows, key_fields=("payment_key", "kind")):
    out: dict[tuple, dict] = defaultdict(
        lambda: {"amount": Decimal(0), "fee": Decimal(0), "method": None, "n": 0}
    )
    for r in rows:
        k = tuple(r[f] for f in key_fields)
        out[k]["amount"] += Decimal(r["amount"])
        out[k]["fee"] += Decimal(r.get("fee") or 0) + Decimal(r.get("vat") or 0)
        out[k]["method"] = r.get("method")
        out[k]["n"] += 1
    return out


def classify(
    settle_date: dt.date,
    internal: list[dict],  # 정산일 D 로 분류된 내부 거래 {payment_key, kind, amount(양수)}
    pg: list[dict],  # D 파일 유효본 {payment_key, kind, amount(취소는 음수), fee, vat, method}
    pg_next_day_keys: set[tuple[str, str]] | None,  # D+1 파일의 (key, kind). None = 아직 파일 없음
    internal_prev_day_keys: set[tuple[str, str]],  # 내부에서 D-1 로 분류된 (key, kind)
    known_payment_keys: set[str],  # 내부 payments 에 존재하는 키 전체(상태 무관)
) -> list[Result]:
    ins = _sum(internal)
    # PG 파일의 취소는 음수. 내부와 같은 부호(양수)로 맞춘다
    pgs = _sum([{**r, "amount": abs(Decimal(r["amount"]))} for r in pg])
    raw_pg = _sum(pg)
    results = []
    for k in sorted(set(ins) | set(pgs)):
        key, kind = k
        i_amt = ins[k]["amount"] if k in ins else None
        p_amt = pgs[k]["amount"] if k in pgs else None
        fee_mismatch = False
        if k in raw_pg and raw_pg[k]["method"] in FEE_RATE:
            expected = expected_fee_incl_vat(raw_pg[k]["amount"], raw_pg[k]["method"])
            fee_mismatch = abs(raw_pg[k]["fee"] - expected) > raw_pg[k]["n"]  # 건별 절사 오차 1원 허용
        if i_amt is not None and p_amt is not None:
            cls, cause = ("MATCHED", None) if i_amt == p_amt else ("AMOUNT_MISMATCH", "AMOUNT_DIFF")
        elif i_amt is not None:
            cls = "INTERNAL_ONLY"
            if pg_next_day_keys is None:
                cause = "PG_PENDING"
            elif k in pg_next_day_keys:
                cause = "TIMING_NEXT_DAY"
            else:
                cause = "PG_MISSING"
        else:
            cls = "PG_ONLY"
            if k in internal_prev_day_keys:
                cause = "TIMING_PREV_DAY"
            elif key in known_payment_keys:
                cause = "INTERNAL_STATUS_LAG"
            else:
                cause = "UNKNOWN_TXN"
        diff = (p_amt or Decimal(0)) - (i_amt or Decimal(0))
        results.append(Result(settle_date, key, kind, i_amt, p_amt, diff, cls, cause,
                              cls == "MATCHED" or cause in EXPLAINED, fee_mismatch))  # fmt: skip
    return results


def summary(results: list[Result]) -> dict:
    by = defaultdict(int)
    for r in results:
        by[r.classification] += 1
    unexplained = [r for r in results if not r.explained and r.cause_code not in PENDING]
    return {
        "total": len(results),
        **{k: by[k] for k in ("MATCHED", "AMOUNT_MISMATCH", "INTERNAL_ONLY", "PG_ONLY")},
        "pending": sum(1 for r in results if r.cause_code in PENDING),
        "unexplained_count": len(unexplained),
        "unexplained_amount": str(sum((abs(r.diff) for r in unexplained), Decimal(0))),
        "fee_mismatch": sum(1 for r in results if r.fee_mismatch),
    }


def render_report(settle_date: dt.date, results: list[Result]) -> str:
    s = summary(results)
    lines = [
        f"# PG 정산 대사 {settle_date} (D+1 09:00 KST 게시)",
        "",
        f"- 전체 {s['total']}건: 일치 {s['MATCHED']} · 금액 불일치 {s['AMOUNT_MISMATCH']} · "
        f"내부에만 {s['INTERNAL_ONLY']} · PG 에만 {s['PG_ONLY']} · 보류 {s['pending']}",
        f"- **설명되지 않은 차이: {s['unexplained_count']}건 / {s['unexplained_amount']}원** (목표 0, NFR-03)",
        f"- 수수료 불일치 플래그: {s['fee_mismatch']}건",
        "",
        "| payment_key | 구분 | 내부 | PG | 차이 | 분류 | 원인 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in results:
        if r.classification != "MATCHED":
            lines.append(f"| {r.payment_key} | {r.kind} | {r.internal_amount} | {r.pg_amount} | {r.diff} | "
                         f"{r.classification} | {r.cause_code} |")  # fmt: skip
    return "\n".join(lines) + "\n"
