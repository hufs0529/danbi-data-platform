import datetime as dt
from collections import defaultdict
from decimal import Decimal

from p1.generator.actions import Click, Ddl, Write
from p1.generator.faults import Faults
from p1.generator.simulate import Simulator
from p1.generator.sinks import MemorySink, op_sql

START = dt.datetime(2026, 10, 29, 0, 0, tzinfo=dt.UTC)


def _run(faults: Faults | None = None, seconds: int = 1800, seed: int = 1) -> MemorySink:
    sim = Simulator(seed=seed, start=START, faults=faults, orders_per_minute=60, clicks_per_second=10)
    sink = MemorySink()
    sink.apply(sim.seed_catalog(n_products=50, n_members=100))
    sink.apply(sim.run(seconds))
    return sink


def test_same_seed_same_stream():
    a, b = _run(seconds=300), _run(seconds=300)
    assert a.counts == b.counts


def test_payment_writes_are_transactional_with_outbox():
    sink = _run()
    for w in sink.writes:
        if w.db != "payment":
            continue
        tables = [op.table for op in w.ops]
        if "payment_status_history" in tables:
            assert "outbox_events" in tables, "상태 변경과 outbox 는 같은 트랜잭션"


def test_payment_versions_strictly_increase_per_key():
    sink = _run(Faults(out_of_order_ratio=0.3))
    versions = defaultdict(list)
    for w in sink.writes:
        for op in w.ops:
            if op.table == "outbox_events":
                versions[op.values["aggregateid"]].append(op.values["payload"]["version"])
    assert versions
    for vs in versions.values():
        assert vs == sorted(set(vs))


def test_out_of_order_produces_capture_before_approval():
    sink = _run(Faults(out_of_order_ratio=1.0), seconds=3600)
    seq = defaultdict(list)
    for w in sink.writes:
        for op in w.ops:
            if op.table == "outbox_events":
                seq[op.values["aggregateid"]].append(op.values["type"])
    reversed_ = [s for s in seq.values() if s[:2] == ["PaymentCaptured", "PaymentApproved"]]
    assert reversed_, "상태 역전 이벤트가 생겨야 한다"


def test_refunds_never_exceed_payment():
    sim = Simulator(seed=3, start=START, orders_per_minute=120, clicks_per_second=0)
    sink = MemorySink()
    sink.apply(sim.seed_catalog(n_products=20, n_members=50))
    sink.apply(sim.run(7200))
    paid: dict[int, Decimal] = {}
    canceled: dict[int, Decimal] = defaultdict(Decimal)
    for w in sink.writes:
        for op in w.ops:
            if op.table == "payments" and op.kind == "insert":
                paid[op.values["payment_id"]] = op.values["amount"]
            if op.table == "refunds":
                canceled[op.values["payment_id"]] += op.values["amount"]
    assert canceled, "취소·부분취소가 발생해야 한다"
    for pid, amt in canceled.items():
        assert amt <= paid[pid]


def test_late_clicks_are_delivered_later_than_event_time():
    sink = _run(Faults(late_ratio=0.2), seconds=600)
    late = [c for c in sink.clicks if (c.deliver_at - c.event["event_time"]) > dt.timedelta(minutes=4)]
    assert late and all(isinstance(c, Click) for c in late)


def test_schema_change_tick():
    sink = _run(Faults(schema_change_at=10), seconds=20)
    assert [d.note for d in sink.ddls if isinstance(d, Ddl)] == ["S-C1 호환 변경: nullable 컬럼 추가"]


def test_member_joined_at_is_naive_kst_legacy():
    sink = _run(seconds=1)
    members = [op for w in sink.writes if isinstance(w, Write) for op in w.ops if op.table == "members"]
    assert members[0].values["joined_at"].tzinfo is None
    assert members[0].values["joined_at"].hour == 9  # 00:00 UTC == 09:00 KST


def test_op_sql_quotes_and_converts_utc_for_mysql():
    sink = _run(seconds=60)
    order = next(op for w in sink.writes for op in w.ops if op.table == "orders" and op.kind == "insert")
    sql, params = op_sql("mysql", "commerce", order)
    assert sql.startswith("INSERT INTO `orders` (`order_id`")
    ordered_at = params[list(order.values).index("ordered_at")]
    assert ordered_at.tzinfo is None


def test_faults_parse():
    f = Faults.parse("late=0.1,dup=0.02,schema_change_at=50")
    assert (f.late_ratio, f.dup_ratio, f.schema_change_at) == (0.1, 0.02, 50)
