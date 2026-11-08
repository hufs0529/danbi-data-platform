"""Bronze → Silver 증분 MERGE. 15분마다(ADR-06: Silver 지연은 분 단위로 감수)."""

from __future__ import annotations

import pendulum
from airflow.sdk import DAG, Asset
from airflow.timetables.trigger import CronTriggerTimetable
from p1_common import DEFAULT_ARGS, spark_job

SILVER = Asset("danbi://lake/silver", group="lakehouse")

with DAG(
    dag_id="p1_silver_merge",
    schedule=CronTriggerTimetable("*/15 * * * *", timezone="Asia/Seoul"),
    start_date=pendulum.datetime(2026, 10, 1, tz="Asia/Seoul"),
    catchup=False,
    max_active_runs=1,  # 같은 테이블 동시 MERGE → 커밋 충돌. 앞 실행이 끝나야 다음
    dagrun_timeout=pendulum.duration(minutes=30),
    default_args=DEFAULT_ARGS,
    render_template_as_native_obj=True,
    params={"runbook": "silver-freshness.md"},
    tags=["p1", "silver"],
) as dag:
    spark_job(
        "silver_merge",
        "silver_merge",
        ["--tables", "all", "--overlap-minutes", "10"],
        inlets=[Asset("danbi://lake/bronze", group="lakehouse")],
        outlets=[SILVER],
    )
