import json
import sys
from pathlib import Path

import pytest

DAGS = Path(__file__).resolve().parents[1] / "dags"
sys.path.insert(0, str(DAGS))

from p1_events import extract_objects, settle_dates  # noqa: E402


def _eb(key, version="v1", bucket="danbi-dev-landing-1"):
    return {"detail-type": "Object Created", "source": "aws.s3",
            "detail": {"bucket": {"name": bucket}, "object": {"key": key, "version-id": version}}}  # fmt: skip


def test_extract_from_sqs_batch_with_string_bodies():
    k1 = "pg_settlement/dt=2026-10-29/danbipay_settle_20261029_01.csv"
    k2 = "pg_settlement/dt=2026-10-29/danbipay_settle_20261029_02.csv"
    extra = {"from_trigger": True, "payload": {"message_batch": [
        {"MessageId": "1", "Body": json.dumps(_eb(k1))},
        {"MessageId": "2", "Body": json.dumps(_eb(k1, version="v2"))},  # 같은 이름 재전송(다른 버전)
        {"MessageId": "3", "Body": json.dumps(_eb(k2))},
        {"MessageId": "4", "Body": json.dumps(_eb("pg_settlement/readme.txt"))},
        {"MessageId": "5", "Body": "not json"},
    ]}}  # fmt: skip
    objs = extract_objects([extra])
    assert [(o["key"], o["version_id"]) for o in objs] == [(k1, "v1"), (k1, "v2"), (k2, "v1")]
    assert settle_dates(objs) == ["2026-10-29"]


def test_extract_ignores_duplicates_of_same_version():
    k = "pg_settlement/dt=2026-10-30/danbipay_settle_20261030_01.csv"
    assert len(extract_objects([_eb(k), _eb(k)])) == 1


def test_dagbag_imports_without_errors(monkeypatch, tmp_path):
    pytest.importorskip("airflow")
    monkeypatch.setenv("AIRFLOW_HOME", str(tmp_path))
    monkeypatch.setenv("AIRFLOW__CORE__LOAD_EXAMPLES", "False")
    from airflow.models.dagbag import DagBag

    bag = DagBag(dag_folder=str(DAGS), include_examples=False)
    assert bag.import_errors == {}
    assert {"p1_silver_merge", "p1_gold_daily", "p1_pg_settlement", "p1_recon_daily", "p1_recon_source",
            "p1_iceberg_maintenance", "p1_backfill"} <= set(bag.dags)  # fmt: skip
    gold = bag.dags["p1_gold_daily"]
    assert gold.get_task("dbt_build_audit").retries == 0  # 테스트 실패는 재시도하지 않는다
    assert gold.render_template_as_native_obj
