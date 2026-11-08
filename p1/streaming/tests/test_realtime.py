import base64
import datetime as dt
import json
from decimal import Decimal

from p1.generator.faults import Faults
from p1.generator.simulate import Simulator
from p1.generator.sinks import MemorySink
from p1.streaming.realtime import alert_lambda, logic

T0 = dt.datetime(2026, 10, 29, 1, 0, 0)


def _e(key, status="APPROVED", amount=10000, card="card_v1_x", addr=1, sec=0, version=1):
    return logic.PayEvent(key, "", status, None if amount is None else Decimal(amount), card, addr, "m", version,
                          T0 + dt.timedelta(seconds=sec))  # fmt: skip


def test_sales_acc_dedups_and_nets():
    acc = logic.SalesAcc()
    for e in [_e("a"), _e("a"), _e("a", "CAPTURED", version=2), _e("b"), _e("b", "PARTIAL_CANCELED", 3000, version=2),
              _e("c", amount=None)]:  # fmt: skip
        acc.add(e)
    row = logic.sales_row(T0, acc, 1, T0)
    assert (row["approved_amount"], row["captured_amount"], row["canceled_amount"]) == (20000, 10000, 3000)
    assert row["net_amount"] == 7000 and row["payments"] == 2 and row["null_amounts"] == 1
    merged = logic.SalesAcc().add(_e("x")).merge(logic.SalesAcc().add(_e("y")))
    assert merged.approved == 20000


def test_fraud_three_addresses_within_five_minutes_once():
    st = logic.FraudState()
    alerts = []
    for i, (addr, sec) in enumerate([(1, 0), (2, 60), (3, 120), (4, 130)]):
        st, a = logic.detect(st, _e(f"k{i}", addr=addr, sec=sec), now_ms=_e("z", sec=sec + 2).epoch_ms)
        alerts.append(a)
    assert alerts[:2] == [None, None] and alerts[2] is not None and alerts[3] is None  # 쿨다운
    assert alerts[2]["distinct_addresses"] == 3 and alerts[2]["detection_lag_ms"] == 2000


def test_fraud_out_of_order_and_outside_window():
    st = logic.FraudState()
    st, a = logic.detect(st, _e("k1", addr=1, sec=600), 0)
    st, a = logic.detect(st, _e("k2", addr=2, sec=0), 0)  # 10분 전 이벤트가 늦게 도착: 같은 5분 창이 아님
    st, a = logic.detect(st, _e("k3", addr=3, sec=590), 0)
    assert a is None
    st, a = logic.detect(st, _e("k4", addr=4, sec=580), 0)  # 늦게 왔지만 같은 창 → 탐지
    assert a is not None and set(a["payment_keys"]) == {"k1", "k3", "k4"}


def test_fraud_same_address_repeated_is_not_fraud():
    st = logic.FraudState()
    for i in range(5):
        st, a = logic.detect(st, _e(f"k{i}", addr=7, sec=i * 10), 0)
        assert a is None


def test_generator_fraud_bursts_are_all_detected():
    """생성기가 주입한 이상결제 카드 100% 탐지(S-B3 수용 기준)."""
    sim = Simulator(seed=11, start=T0.replace(tzinfo=dt.UTC), faults=Faults(fraud_burst_ratio=0.01),
                    orders_per_minute=20, clicks_per_second=0)  # fmt: skip
    sink = MemorySink()
    sink.apply(sim.seed_catalog(n_products=30, n_members=200))
    sink.apply(sim.run(1800))
    states: dict[str, logic.FraudState] = {}
    flagged = set()
    for w in sink.writes:
        for op in w.ops:
            if op.table != "outbox_events":
                continue
            pl = op.values["payload"]
            ev = logic.PayEvent(pl["payment_key"], op.values["type"], pl["to_status"], Decimal(pl["amount"]),
                                pl["card_token"], pl["address_id"], None, pl["version"],
                                dt.datetime.fromisoformat(pl["occurred_at"]).replace(tzinfo=None))  # fmt: skip
            if ev.card_token is None:
                continue
            st, alert = logic.detect(states.get(ev.card_token, logic.FraudState()), ev, 0)
            states[ev.card_token] = st
            if alert:
                flagged.add(alert["card_token"])
    assert sim.fraud_cards and set(sim.fraud_cards) <= flagged
    assert all(c.startswith("ctk_stolen") for c in flagged)  # 정상 카드 오탐 0


def test_lambda_parse_and_metrics():
    def rec(obj):
        return {"value": base64.b64encode(json.dumps(obj).encode()).decode()}

    alert = {"alert_id": "a1", "card_token": "c"}
    sale = {"window_start": "2026-10-29 01:00:00", "net_amount": "7000", "approved_amount": "20000", "payments": 2,
            "null_amounts": 0}  # fmt: skip
    event = {"records": {"alerts.fraud-0": [rec(alert), rec(alert)], "rt.sales_1m-1": [rec(sale)]}}
    alerts, sales = alert_lambda.parse(event)
    assert len(alerts) == 1 and len(sales) == 1
    data = {d["MetricName"]: d["Value"] for d in alert_lambda.metric_data(sales)}
    assert data["NetSales1m"] == 7000.0 and data["Payments1m"] == 2.0
