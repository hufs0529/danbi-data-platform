"""매일 Gold: Silver 신선도 확인 → dbt build(gold_audit, 테스트) → 게시(gold) — Write-Audit-Publish.

게시 기한 07:00 KST(NFR-02). 05:30 시작 → 06:30 까지 끝나지 않으면 경고(SLO 알림은 P1-14).
"""

from __future__ import annotations

import pendulum
from airflow.providers.standard.sensors.python import PythonSensor
from airflow.sdk import DAG, Asset
from airflow.timetables.trigger import CronTriggerTimetable
from p1_common import CFG, DEFAULT_ARGS, athena_scalar, ecs_task, spark_job

GOLD = Asset("danbi://lake/gold", group="lakehouse")
RUN = "{{ run_id | replace(':', '-') | replace('+', '-') }}"


def silver_is_fresh() -> bool:
    """Silver MERGE 워터마크가 20분 이내면 진행. 오래됐으면 기다린다(센서 재예약)."""
    lag = athena_scalar(
        "SELECT CAST(date_diff('minute', max(updated_at), current_timestamp) AS varchar) "
        "FROM ops.pipeline_watermarks WHERE pipeline = 'silver_merge'"
    )
    return lag is not None and int(lag) <= 20


with DAG(
    dag_id="p1_gold_daily",
    schedule=CronTriggerTimetable("30 5 * * *", timezone="Asia/Seoul"),
    start_date=pendulum.datetime(2026, 10, 1, tz="Asia/Seoul"),
    catchup=False,
    max_active_runs=1,
    dagrun_timeout=pendulum.duration(hours=2),
    default_args=DEFAULT_ARGS,
    render_template_as_native_obj=True,
    params={"runbook": "gold-publish.md"},
    tags=["p1", "gold", "wap"],
) as dag:
    wait = PythonSensor(task_id="silver_fresh", python_callable=silver_is_fresh, mode="reschedule",
                        poke_interval=300, timeout=3600)  # fmt: skip

    audit = ecs_task(
        "dbt_build_audit",
        "dbt_task_family",
        ["build", "--select", "tag:publish"],
        env={
            "DBT_RUN_ID": RUN,
            "OPENLINEAGE_URL": "{{ " + CFG + ".marquez_url }}",
            "OPENLINEAGE_NAMESPACE": "danbi-dev",
        },
        retries=0,  # 테스트 실패는 재시도해도 같다. 실패 = 게시 보류 + 알림
    )

    publish = spark_job(
        "publish_gold",
        "publish_gold",
        ["--run-results", "s3://{{ " + CFG + ".artifacts_bucket }}/dbt/runs/" + RUN + "/run_results.json"],
        outlets=[GOLD],
    )

    wait >> audit >> publish
