import datetime as dt
import json

import pytest

from contracts.registry import get
from p1.batch import transforms as T
from p1.batch.jobs.maintenance import maintenance_sql

pytestmark = pytest.mark.spark

TS = dt.datetime(2026, 10, 29, 1, 0, 0)


def _bronze(spark, rows):
    """rows: (op, seq, after|before dict, txn, ts_offset_min)"""
    data = []
    for op, seq, image, txn, off in rows:
        j = json.dumps(image)
        data.append(
            (op, None if op == "d" else j, j if op == "d" else None, seq, txn, TS + dt.timedelta(minutes=off))
        )
    return spark.createDataFrame(data, "op string, after_json string, before_json string, change_seq string, "
                                       "txn_id string, source_ts timestamp")  # fmt: skip


def _order(oid, status, total="10000", ordered_at="2026-10-28T15:30:00Z"):
    return {"order_id": oid, "order_no": f"O{oid}", "member_id": "member_v1_x", "address_id": 1, "status": status,
            "total_amount": total, "discount_amount": "0", "coupon_id": None, "currency": "KRW",
            "ordered_at": ordered_at, "updated_at": ordered_at}  # fmt: skip


def test_cdc_to_rows_casts_and_derives_kst_date(spark):
    df = T.cdc_to_rows(_bronze(spark, [("c", "m001", _order(1, "CREATED"), "t1", 0)]), get("silver.orders"))
    r = df.collect()[0]
    assert r.order_id == 1 and str(r.total_amount) == "10000"
    assert r.ordered_at == dt.datetime(2026, 10, 28, 15, 30)
    assert r.ordered_date_kst == dt.date(2026, 10, 29)  # 15:30 UTC = 다음 날 00:30 KST
    assert r.member_token == "member_v1_x" and r._src_seq == "m001"


def test_latest_per_key_uses_source_order_not_arrival(spark):
    # 늦게 도착했지만 소스 순서가 앞선 변경(m002)이 최신(m003)을 덮으면 안 된다
    rows = [("c", "m001", _order(1, "CREATED"), "t1", 0), ("u", "m003", _order(1, "PAID"), "t3", 1),
            ("u", "m002", _order(1, "CREATED"), "t2", 5)]  # fmt: skip
    df = T.latest_per_key(T.cdc_to_rows(_bronze(spark, rows), get("silver.orders")), ["order_id"])
    assert [(r.status, r._src_seq) for r in df.collect()] == [("PAID", "m003")]


def test_delete_reads_before_image(spark):
    df = T.cdc_to_rows(_bronze(spark, [("d", "m009", _order(1, "PAID"), "t9", 0)]), get("silver.orders"))
    r = df.collect()[0]
    assert r.op == "d" and r.order_id == 1


def test_legacy_kst_datetime_converted_to_utc(spark):
    member = {"member_id": "member_v1_a", "email": "email_v1_x", "name": "enc:v1:zz", "phone": "phone_v1_y",
              "birth_date__birth_year": 1990, "grade": "GOLD", "marketing_agreed": 1, "status": "ACTIVE",
              "joined_at": 1793271600000, "withdrawn_at": None, "updated_at": "2026-10-29T01:00:00Z"}  # fmt: skip
    # 1793271600000 ms = 2026-10-29T11:00:00 '로 저장된 KST 벽시계' → 실제 UTC 02:00
    r = T.cdc_to_rows(_bronze(spark, [("c", "m1", member, "t", 0)]), get("silver.members")).collect()[0]
    assert r.joined_at == dt.datetime(2026, 10, 29, 2, 0)
    assert r.birth_year == 1990 and r.marketing_agreed is True


def test_unknown_fields_detects_drift(spark):
    o = _order(1, "CREATED") | {"gift_message": "생일 축하"}
    assert T.unknown_fields(_bronze(spark, [("c", "m1", o, "t", 0)]), get("silver.orders")) == [
        "gift_message"
    ]


def _member_change(grade, status="ACTIVE"):
    return {"member_id": "member_v1_a", "grade": grade, "status": status, "marketing_agreed": 1}


def _current(spark, rows):
    return spark.createDataFrame(rows, "member_token string, grade string, status string, marketing_agreed boolean, "
                                       "valid_from timestamp, _src_seq string")  # fmt: skip


def test_scd2_versions_new_member_and_same_txn_collapse(spark):
    c = get("silver.dim_member_scd2")
    rows = [("c", "m1", _member_change("BASIC"), "tx1", 0), ("u", "m2", _member_change("SILVER"), "tx1", 0),
            ("u", "m3", _member_change("GOLD"), "tx2", 30)]  # fmt: skip
    changes = T.cdc_to_rows(_bronze(spark, rows), c)
    out = T.scd2_versions(changes, _current(spark, []), c).orderBy("valid_from").collect()
    # 같은 트랜잭션(tx1)의 BASIC→SILVER 는 SILVER 하나로
    assert [(r.grade, r.is_current) for r in out] == [("SILVER", False), ("GOLD", True)]
    assert out[0].valid_to == out[1].valid_from


def test_scd2_closes_current_and_is_idempotent(spark):
    c = get("silver.dim_member_scd2")
    current = _current(spark, [("member_v1_a", "SILVER", "ACTIVE", True, TS, "m2")])
    changes = T.cdc_to_rows(_bronze(spark, [("u", "m2", _member_change("SILVER"), "tx1", 0),   # 이미 반영됨
                                            ("u", "m5", _member_change("VIP"), "tx5", 60)]), c)  # fmt: skip
    out = {r.grade: r for r in T.scd2_versions(changes, current, c).collect()}
    assert set(out) == {"SILVER", "VIP"}
    assert out["SILVER"].is_current is False and out["SILVER"].valid_to == TS + dt.timedelta(minutes=60)
    assert out["VIP"].is_current is True


def test_scd2_ignores_untracked_only_changes(spark):
    c = get("silver.dim_member_scd2")
    current = _current(spark, [("member_v1_a", "GOLD", "ACTIVE", True, TS, "m2")])
    changes = T.cdc_to_rows(_bronze(spark, [("u", "m7", _member_change("GOLD"), "tx7", 10)]), c)
    out = T.scd2_versions(changes, current, c).collect()
    assert len(out) == 1 and out[0].is_current and out[0].valid_to is None


def test_merge_sql_guards_on_source_sequence():
    sql = T.merge_sql(get("silver.orders"), "src", "run-1", extra=("gift_message",))
    assert "WHEN MATCHED AND s._src_seq > t._src_seq AND s.op = 'd'" in sql
    assert "t.gift_message = s.gift_message" in sql
    assert "MERGE INTO glue.silver.orders t" in sql


def test_maintenance_never_uses_s3_lifecycle_and_deletes_bronze_by_table_op():
    stmts = maintenance_sql("bronze.cdc_events", dt.datetime(2026, 10, 30))
    assert stmts[0] == "DELETE FROM glue.bronze.cdc_events WHERE ingest_ts < TIMESTAMP '2026-08-01 00:00:00'"
    assert any("expire_snapshots" in s for s in stmts) and any("remove_orphan_files" in s for s in stmts)
    assert not maintenance_sql("silver.orders", dt.datetime(2026, 10, 30))[0].startswith("DELETE")
