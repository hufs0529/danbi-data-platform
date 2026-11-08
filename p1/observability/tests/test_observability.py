import datetime as dt

from p1.observability import monitor as M
from p1.observability.slo_report import attainment, budget, is_good, render

NOW = dt.datetime(2026, 10, 29, 22, 0, tzinfo=dt.UTC)  # 07:00 KST(다음 날)


def _md(snapshot_utc: dt.datetime | None):
    if snapshot_utc is None:
        return {"current-snapshot-id": -1, "snapshots": []}
    return {"current-snapshot-id": 7, "snapshots": [{"snapshot-id": 6, "timestamp-ms": 1},
            {"snapshot-id": 7, "timestamp-ms": int(snapshot_utc.timestamp() * 1000)}]}  # fmt: skip


def test_freshness_from_metadata():
    assert M.freshness_seconds(_md(NOW - dt.timedelta(minutes=12)), NOW) == 720
    assert M.freshness_seconds(_md(None), NOW) is None


def test_gold_deadline():
    published_today = dt.datetime(2026, 10, 30, 6, 10, tzinfo=M.KST)
    assert M.gold_deadline_breached(_md(published_today), NOW) == 0
    yesterday = dt.datetime(2026, 10, 29, 6, 10, tzinfo=M.KST)
    assert M.gold_deadline_breached(_md(yesterday), NOW) == 1
    early = dt.datetime(2026, 10, 29, 20, 0, tzinfo=dt.UTC)  # 05:00 KST 는 아직 기한 전
    assert M.gold_deadline_breached(_md(yesterday), early) == 0


def test_connectors_and_small_files_sql():
    assert M.connectors_running(["RUNNING", "RUNNING"]) == 1
    assert M.connectors_running(["RUNNING", "FAILED"]) == 0 and M.connectors_running([]) == 0
    assert M.small_file_sql("silver.orders").endswith('FROM "silver"."orders$files"')


def test_config_every_alert_has_runbook_and_valid_rule():
    cfg = M.load_config()
    for s in cfg["slos"]:
        assert s["runbook"].endswith(".md") and 0 < s["target_ratio"] <= 1
        is_good(s["good_when"], 0)
    for a in cfg["alerts"]:
        assert a["runbook"].endswith(".md")


def test_attainment_counts_missing_as_bad_and_budget_freeze():
    values = [100.0] * 97 + [None, 5000.0, 5000.0]
    a = attainment(values, "<= 900")
    assert a == 0.97
    b = budget(0.99, a)
    assert b["freeze"] and b["budget_remaining"] == 0
    ok = budget(0.99, 0.995)
    assert not ok["freeze"] and ok["budget_remaining"] == 0.5
    assert "기능 작업 중지" in render(
        "2026-10", [{"product": "silver.orders", "objective": "x", "actual": a, **b}]
    )
