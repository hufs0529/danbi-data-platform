"""대사 1단계: 소스 DB vs 레이크 완전성 (NFR-04, F-01 유실 0 검증).

창(window) 안에서 갱신된 행의 수와 금액 합을 소스와 Silver 에서 각각 세어 비교한다.
- 창 끝은 '지금 - 지연 여유'(기본 30분)로 둬서 아직 레이크에 도착 중인 변경 때문에 생기는 거짓 경보를 막는다.
- 행 수만 보면 같은 수의 다른 행이 섞여도 모른다 → 금액 합(체크섬 대용)을 함께 본다.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Check:
    source_db: str
    table: str
    silver: str
    ts_col: str
    amount_col: str | None


CHECKS = [
    Check("commerce", "orders", "silver.orders", "updated_at", "total_amount"),
    Check("commerce", "order_items", "silver.order_items", "updated_at", "unit_price"),
    Check("commerce", "members", "silver.members", "updated_at", None),
    Check("payment", "payments", "silver.payments", "updated_at", "amount"),
    Check("payment", "payment_status_history", "silver.payment_status_history", "occurred_at", "amount"),
]


def source_sql(c: Check, engine: str) -> str:
    amount = f"COALESCE(SUM({c.amount_col}), 0)" if c.amount_col else "0"
    return f"SELECT COUNT(*), {amount} FROM {c.table} WHERE {c.ts_col} >= %s AND {c.ts_col} < %s"


@dataclass(frozen=True)
class Outcome:
    check: Check
    window_start: dt.datetime
    window_end: dt.datetime
    source_count: int
    lake_count: int
    source_amount: Decimal
    lake_amount: Decimal

    @property
    def ok(self) -> bool:
        return self.source_count == self.lake_count and self.source_amount == self.lake_amount


def window(now: dt.datetime, minutes: int = 60, lag_minutes: int = 30) -> tuple[dt.datetime, dt.datetime]:
    end = (now - dt.timedelta(minutes=lag_minutes)).replace(second=0, microsecond=0)
    end = end - dt.timedelta(minutes=end.minute % minutes)
    return end - dt.timedelta(minutes=minutes), end
