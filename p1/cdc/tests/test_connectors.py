import json

import pytest

from p1.cdc.lint import expected_topics, lint, load_connector, render_template
from p1.cdc.signal import insert_sql, snapshot_signal
from p1.streaming.topics import load_specs

KNOWN = {s.name for s in load_specs()}


def test_template_escape_matches_terraform():
    out = render_template('{"a": "${x}", "b": "$${secretsmanager:${x}:pw}"}', {"x": "v"})
    assert json.loads(out) == {"a": "v", "b": "${secretsmanager:v:pw}"}


def test_template_missing_var_fails():
    with pytest.raises(KeyError):
        render_template("${nope}", {})


@pytest.mark.parametrize("name", ["mysql-commerce", "pg-payment"])
def test_repository_connectors_pass_lint(name):
    assert lint(name, load_connector(name), KNOWN) == []


def test_outbox_routes_to_events_topic_not_raw_table_topic():
    topics = expected_topics(load_connector("pg-payment"))
    assert "events.payment" in topics
    assert "cdc.pg.public.outbox_events" not in topics
    assert "__debezium-heartbeat.cdc.pg" in topics


def test_lint_catches_missing_topic_and_plaintext_password():
    cfg = load_connector("mysql-commerce")
    cfg["database.password"] = "hunter2"
    errs = lint("x", cfg, KNOWN - {"__debezium-heartbeat.cdc.mysql"})
    assert any("config provider" in e for e in errs)
    assert any("__debezium-heartbeat.cdc.mysql" in e for e in errs)


def test_lint_catches_locking_snapshot_on_rds():
    cfg = load_connector("mysql-commerce")
    cfg["snapshot.locking.mode"] = "minimal"
    assert any("snapshot.locking.mode" in e for e in lint("x", cfg, KNOWN))


def test_pg_requires_heartbeat_query_and_keeps_slot():
    cfg = load_connector("pg-payment")
    del cfg["heartbeat.action.query"]
    cfg["slot.drop.on.stop"] = "true"
    errs = lint("x", cfg, KNOWN)
    assert any("heartbeat.action.query" in e for e in errs)
    assert any("slot.drop.on.stop" in e for e in errs)


def test_incremental_snapshot_signal_with_filter():
    sig = snapshot_signal(["commerce.orders"], "updated_at >= '2026-10-29'")
    data = json.loads(sig["data"])
    assert sig["type"] == "execute-snapshot" and data["type"] == "incremental"
    assert data["additional-conditions"][0]["filter"].startswith("updated_at")
    assert insert_sql("postgres").startswith("INSERT INTO public.debezium_signal")
    with pytest.raises(ValueError):
        snapshot_signal(["commerce.orders"], "1=1; DROP TABLE orders")
