"""정기 운영 DAG 묶음: 매일 대사(08:30), 매시 소스-레이크 대사, 매일 Iceberg 유지보수(03:30)."""

from __future__ import annotations

import pendulum
from airflow.sdk import DAG
from airflow.timetables.trigger import CronTriggerTimetable
from p1_common import DEFAULT_ARGS, ecs_task, spark_job

START = pendulum.datetime(2026, 10, 1, tz="Asia/Seoul")
COMMON = {"start_date": START, "catchup": False, "max_active_runs": 1, "default_args": DEFAULT_ARGS,
          "render_template_as_native_obj": True}  # fmt: skip

with DAG(
    dag_id="p1_recon_daily",
    schedule=CronTriggerTimetable("30 8 * * *", timezone="Asia/Seoul"),
    params={"runbook": "pg-settlement.md"},
    tags=["p1", "recon"],
    **COMMON,
):
    # 어제(D) 와 그제(D-1, 보류 건 재판정). 09:00 재무 보고 마감 30분 전
    ecs_task("recon_pg_yesterday", "recon_task_family", ["recon-pg", "--date", "{{ macros.ds_add(ds, -1) }}"],
             retries=1)  # fmt: skip

with DAG(
    dag_id="p1_recon_source",
    schedule=CronTriggerTimetable("40 * * * *", timezone="Asia/Seoul"),
    params={"runbook": "cdc-completeness.md"},
    tags=["p1", "recon", "cdc"],
    **COMMON,
):
    ecs_task("recon_source", "recon_task_family", ["recon-source"], retries=1)

with DAG(
    dag_id="p1_iceberg_maintenance",
    schedule=CronTriggerTimetable("30 3 * * *", timezone="Asia/Seoul"),
    params={"runbook": "small-files.md"},
    tags=["p1", "maintenance"],
    **COMMON,
):
    spark_job("maintenance", "maintenance", ["--tables", "all", "--snapshot-days", "7"])
