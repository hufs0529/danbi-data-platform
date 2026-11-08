"""데이터 관측 Lambda (boto3 만 사용, slos.yaml 은 배포 시 함께 묶는다).

    mode=freshness (5분): Iceberg 메타데이터의 현재 스냅샷 시각 → FreshnessSeconds{Table}
                         Gold 게시 기한(07:00 KST, 경고 06:30) → GoldDeadlineBreached
                         MSK Connect 커넥터 상태 → ConnectorRunning(모두 RUNNING 이면 1)
    mode=health    (매일): 작은 파일 비율(Athena "$files") → SmallFileRatio{Table}  (NFR-11)

신선도를 쿼리가 아니라 메타데이터로 재는 이유: 5분마다 테이블을 스캔하면 비용이 들고, 쿼리 실패가 '데이터가 늦다'와
구별되지 않는다. Iceberg 는 커밋마다 메타데이터 파일에 스냅샷 시각을 남긴다.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

KST = dt.timezone(dt.timedelta(hours=9))
GOLD_WARN = dt.time(6, 30)


def snapshot_time_ms(metadata: dict) -> int | None:
    cur = metadata.get("current-snapshot-id")
    if cur is None or cur == -1:
        return None
    for s in metadata.get("snapshots", []):
        if s.get("snapshot-id") == cur:
            return int(s["timestamp-ms"])
    return None


def freshness_seconds(metadata: dict, now: dt.datetime) -> float | None:
    ts = snapshot_time_ms(metadata)
    return None if ts is None else max(0.0, now.timestamp() - ts / 1000)


def gold_deadline_breached(metadata: dict, now: dt.datetime) -> int:
    """06:30 KST 이후인데 오늘(KST) 게시된 스냅샷이 없으면 1."""
    local = now.astimezone(KST)
    if local.time() < GOLD_WARN:
        return 0
    ts = snapshot_time_ms(metadata)
    if ts is None:
        return 1
    published = dt.datetime.fromtimestamp(ts / 1000, KST)
    return 0 if published.date() == local.date() else 1


def small_file_sql(table: str, threshold_bytes: int = 32 * 1024 * 1024) -> str:
    db, t = table.split(".")
    return (f'SELECT CAST(count_if(file_size_in_bytes < {threshold_bytes}) AS double) / nullif(count(*), 0) '
            f'FROM "{db}"."{t}$files"')  # fmt: skip


def connectors_running(states: list[str]) -> int:
    return 1 if states and all(s == "RUNNING" for s in states) else 0


def load_config() -> dict:
    """Lambda 에서는 배포 때 만든 slos.json(PyYAML 없음), 저장소에서는 slos.yaml."""
    js = Path(__file__).with_name("slos.json")
    if js.exists():
        return json.loads(js.read_text(encoding="utf-8"))
    import yaml

    return yaml.safe_load((Path(__file__).with_name("slos.yaml")).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
def _metadata(glue, s3, table: str) -> dict | None:
    db, name = table.split(".")
    try:
        loc = glue.get_table(DatabaseName=db, Name=name)["Table"]["Parameters"]["metadata_location"]
    except Exception:
        return None
    bucket, key = loc[5:].split("/", 1)
    return json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())


def handler(event, _context):
    import boto3

    cfg = load_config()
    cw = boto3.client("cloudwatch")
    now = dt.datetime.now(dt.UTC)
    data = []
    if event.get("mode", "freshness") == "freshness":
        glue, s3 = boto3.client("glue"), boto3.client("s3")
        for table in cfg["monitored_tables"]:
            md = _metadata(glue, s3, table)
            if md is None:
                continue
            f = freshness_seconds(md, now)
            if f is not None:
                data.append({"MetricName": "FreshnessSeconds", "Dimensions": [{"Name": "Table", "Value": table}],
                             "Value": f, "Unit": "Seconds"})  # fmt: skip
            if table == "gold.mart_daily_sales":
                data.append({"MetricName": "GoldDeadlineBreached", "Dimensions": [{"Name": "Table", "Value": table}],
                             "Value": gold_deadline_breached(md, now)})  # fmt: skip
        kc = boto3.client("kafkaconnect")
        states = [
            c["connectorState"]
            for c in kc.list_connectors(connectorNamePrefix=os.environ.get("CONNECTOR_PREFIX", ""))[
                "connectors"
            ]
        ]
        cw.put_metric_data(Namespace="Danbi/Pipeline", MetricData=[{"MetricName": "ConnectorRunning",
                                                                     "Value": connectors_running(states)}])  # fmt: skip
        for i in range(0, len(data), 20):
            cw.put_metric_data(Namespace="Danbi/Freshness", MetricData=data[i : i + 20])
        return {"tables": len(data), "connectors": states}

    # health
    import time

    athena = boto3.client("athena")
    out = {}
    for table in cfg["small_file_tables"]:
        qid = athena.start_query_execution(
            QueryString=small_file_sql(table), WorkGroup=os.environ["ATHENA_WORKGROUP"]
        )["QueryExecutionId"]
        while (
            st := athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"]["State"]
        ) in ("QUEUED", "RUNNING"):
            time.sleep(2)
        if st != "SUCCEEDED":
            continue
        rows = athena.get_query_results(QueryExecutionId=qid)["ResultSet"]["Rows"]
        ratio = float(rows[1]["Data"][0].get("VarCharValue") or 0)
        out[table] = ratio
        cw.put_metric_data(Namespace="Danbi/Lake", MetricData=[{"MetricName": "SmallFileRatio",
                           "Dimensions": [{"Name": "Table", "Value": table}], "Value": ratio}])  # fmt: skip
    return out
