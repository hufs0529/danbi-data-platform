from contracts.registry import get
from p1.lakehouse.migrate import alter_sql, plan_contract, render_sql_files

BUCKETS = {"bronze_bucket": "b", "silver_bucket": "s", "gold_bucket": "g"}


def test_sql_files_render_with_buckets():
    files = render_sql_files(BUCKETS)
    assert [v for v, _, _ in files][:2] == ["001_ops", "002_bronze"]
    stmts = [s for _, _, ss in files for s in ss]
    assert all("{" not in s for s in stmts)
    assert any("LOCATION 's3://b/cdc_events/'" in s for s in stmts)
    assert any(s.startswith("CREATE TABLE IF NOT EXISTS ops.schema_migrations") for s in stmts)


def test_new_table_is_created_from_contract():
    tp = plan_contract(get("silver.orders"), None, "s")
    assert tp.create and tp.create.startswith("CREATE TABLE IF NOT EXISTS silver.orders")


def _existing(dataset, drop=(), retype=None):
    from contracts.registry import silver_columns

    cols = [(n, t) for n, t in silver_columns(get(dataset)) if n not in drop]
    if retype:
        cols = [(n, retype[1] if n == retype[0] else t) for n, t in cols]
    return cols


def test_added_contract_column_becomes_alter():
    tp = plan_contract(get("silver.orders"), _existing("silver.orders", drop=("coupon_id",)), "s")
    assert tp.errors == [] and tp.add_columns == [("coupon_id", "bigint")]
    assert alter_sql(tp) == "ALTER TABLE silver.orders ADD COLUMNS (coupon_id bigint)"


def test_type_change_is_refused():
    tp = plan_contract(
        get("silver.orders"), _existing("silver.orders", retype=("discount_amount", "string")), "s"
    )
    assert any("타입 변경" in e for e in tp.errors)


def test_unknown_column_error_unless_pending_drift():
    existing = _existing("silver.orders") + [("gift_message", "string")]
    assert any("계약에서 빠졌다" in e for e in plan_contract(get("silver.orders"), existing, "s").errors)
    tp = plan_contract(get("silver.orders"), existing, "s", frozenset({"gift_message"}))
    assert tp.errors == [] and tp.pending == ["gift_message"]


def test_type_normalization_ignores_spaces():
    existing = [(n, t.replace(",", ", ")) for n, t in _existing("silver.orders")]
    assert plan_contract(get("silver.orders"), existing, "s").errors == []
