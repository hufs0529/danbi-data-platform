from p1.batch.jobs.publish_gold import gate, publish_sql, schema_diff


def _rr(*items):
    return {"results": [{"unique_id": u, "status": s} for u, s in items], "metadata": {"invocation_id": "i"}}


def test_gate_publishes_only_when_everything_passed():
    ok = _rr(("model.danbi.fct_payments", "success"), ("test.danbi.unique_x", "pass"),
             ("model.danbi.mart_daily_sales", "success"))  # fmt: skip
    assert gate(ok) == (["fct_payments", "mart_daily_sales"], [])
    bad = _rr(("model.danbi.fct_payments", "success"), ("test.danbi.null_ratio_below_x", "fail"))
    models, failures = gate(bad)
    assert models == [] and failures == [
        "test.danbi.null_ratio_below_x: fail"
    ]  # F-08: 하나라도 실패하면 전부 보류


def test_schema_diff_allows_add_only():
    gold = [("a", "int"), ("b", "string")]
    assert schema_diff([("a", "int"), ("b", "string"), ("c", "bigint")], gold) == ([("c", "bigint")], [])
    _, errors = schema_diff([("a", "bigint"), ("b", "string")], gold)
    assert errors == ["a: int → bigint"]
    _, errors = schema_diff([("a", "int")], gold)
    assert errors == ["b: 삭제됨"]
    assert schema_diff([("a", "int")], None) == ([], [])


def test_publish_is_single_statement():
    assert publish_sql("x", True) == "INSERT OVERWRITE glue.gold.x SELECT * FROM glue.gold_audit.x"
    assert publish_sql("x", False).startswith("CREATE TABLE glue.gold.x USING iceberg AS SELECT")
