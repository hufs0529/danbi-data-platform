"""실시간 로직(순수 파이썬). Flink 연산자는 이 함수들을 감싸기만 한다 → Flink 없이 테스트.

S-B1 실시간 매출(속보치): 1분 텀블링, 이벤트 시간, 워터마크 30초, 허용 지연 2분(그 안의 늦은 이벤트는 창을 다시 내보냄)
S-B3 이상결제: 같은 카드 토큰으로 5분 안에 서로 다른 배송지 3곳 이상 승인 → 알림(카드당 5분 쿨다운)
F-05 지연 이벤트: 허용 지연을 넘긴 이벤트는 사이드 출력 → rt.late_events. 확정치는 배치(Gold)가 Silver 에서 다시 계산하므로 100% 반영된다
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from decimal import Decimal

WINDOW = dt.timedelta(minutes=1)
FRAUD_WINDOW = dt.timedelta(minutes=5)
FRAUD_DISTINCT_ADDRESSES = 3
FRAUD_COOLDOWN = dt.timedelta(minutes=5)
CANCELS = {"PARTIAL_CANCELED", "CANCELED"}


@dataclass(frozen=True)
class PayEvent:
    payment_key: str
    event_type: str
    to_status: str
    amount: Decimal | None
    card_token: str | None
    address_id: int | None
    member_token: str | None
    version: int
    occurred_at: dt.datetime  # UTC, tz 없음

    @property
    def epoch_ms(self) -> int:
        return int(self.occurred_at.replace(tzinfo=dt.UTC).timestamp() * 1000)


def parse_event(row: dict) -> PayEvent | None:
    """Decoder.domain_event 결과(payload 는 이미 PII 처리됨) → PayEvent."""
    p = json.loads(row["payload_json"]) if row.get("payload_json") else {}
    if not p.get("payment_key") or not p.get("to_status"):
        return None
    amt = p.get("amount")
    return PayEvent(
        payment_key=p["payment_key"], event_type=row.get("event_type") or "", to_status=p["to_status"],
        amount=Decimal(str(amt)) if amt is not None else None, card_token=p.get("card_token"),
        address_id=p.get("address_id"), member_token=p.get("member_id"), version=int(p.get("version") or 0),
        occurred_at=row["occurred_at"],
    )  # fmt: skip


# ---------------------------------------------------------------------------
# 1분 매출
# ---------------------------------------------------------------------------
@dataclass
class SalesAcc:
    approved: Decimal = Decimal(0)
    captured: Decimal = Decimal(0)
    canceled: Decimal = Decimal(0)
    payments: int = 0
    null_amounts: int = 0
    seen: set = field(default_factory=set)  # (payment_key, version) 중복 제거(outbox 재전송)

    def add(self, e: PayEvent) -> SalesAcc:
        ident = (e.payment_key, e.version)
        if ident in self.seen:
            return self
        self.seen.add(ident)
        if e.amount is None:
            self.null_amounts += 1  # F-08 신호. 속보치는 계속 내보내되 개수를 같이 싣는다
            return self
        if e.to_status == "APPROVED":
            self.approved += e.amount
            self.payments += 1
        elif e.to_status == "CAPTURED":
            self.captured += e.amount
        elif e.to_status in CANCELS:
            self.canceled += e.amount
        return self

    def merge(self, other: SalesAcc) -> SalesAcc:
        out = SalesAcc(seen=self.seen | other.seen)
        out.approved, out.captured = self.approved + other.approved, self.captured + other.captured
        out.canceled, out.payments = self.canceled + other.canceled, self.payments + other.payments
        out.null_amounts = self.null_amounts + other.null_amounts
        return out


def window_start(t: dt.datetime) -> dt.datetime:
    return t.replace(second=0, microsecond=0)


def sales_row(start: dt.datetime, acc: SalesAcc, revision: int, emitted_at: dt.datetime) -> dict:
    return {
        "window_start": start, "window_end": start + WINDOW,
        "approved_amount": acc.approved, "captured_amount": acc.captured, "canceled_amount": acc.canceled,
        "net_amount": acc.captured - acc.canceled, "payments": acc.payments, "null_amounts": acc.null_amounts,
        "revision": revision, "emitted_at": emitted_at, "label": "속보치",
    }  # fmt: skip


# ---------------------------------------------------------------------------
# 이상결제
# ---------------------------------------------------------------------------
@dataclass
class FraudState:
    entries: list[tuple[int, int | None, str]] = field(
        default_factory=list
    )  # (epoch_ms, address_id, payment_key)
    last_alert_ms: int | None = None


def detect(state: FraudState, e: PayEvent, now_ms: int) -> tuple[FraudState, dict | None]:
    """승인 이벤트 하나를 반영하고, 규칙에 걸리면 알림을 돌려준다. 순서가 뒤바뀌어 와도 이벤트 시간으로 판단한다."""
    if e.to_status != "APPROVED" or not e.card_token:
        return state, None
    t = e.epoch_ms
    w = int(FRAUD_WINDOW.total_seconds() * 1000)
    if all(pk != e.payment_key for _, _, pk in state.entries):
        state.entries.append((t, e.address_id, e.payment_key))
    # 가장 최근 이벤트 기준으로 창보다 오래된 항목은 버린다(상태 크기 상한)
    newest = max(ts for ts, _, _ in state.entries)
    state.entries = [x for x in state.entries if x[0] > newest - w]
    near = [x for x in state.entries if abs(x[0] - t) <= w]
    addresses = {a for _, a, _ in near if a is not None}
    cooled = state.last_alert_ms is None or t - state.last_alert_ms >= int(
        FRAUD_COOLDOWN.total_seconds() * 1000
    )
    if len(addresses) >= FRAUD_DISTINCT_ADDRESSES and cooled:
        state.last_alert_ms = t
        alert = {
            "alert_id": f"fraud-{e.card_token}-{t}", "rule": "S-B3 same-card multi-address v1",
            "card_token": e.card_token, "distinct_addresses": len(addresses),
            "payment_keys": sorted(pk for _, _, pk in near), "window_minutes": 5,
            "event_time_ms": t, "detected_at_ms": now_ms, "detection_lag_ms": max(0, now_ms - t),
        }  # fmt: skip
        return state, alert
    return state, None
