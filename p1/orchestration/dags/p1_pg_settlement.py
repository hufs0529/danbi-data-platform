"""PG 정산 파일 도착 → 적재·판정 → 대사 (이벤트 기반, Airflow 3 AssetWatcher).

S3(landing) ─EventBridge→ SQS ─MessageQueueTrigger→ 이 DAG. 파일이 여러 개면 파일마다 적재 태스크를 만든다(동적 매핑).
별도로 매일 08:30 KST 에 전날 대사를 한 번 더 돈다(p1_recon_daily) — 파일이 안 와도 '안 왔다'는 사실을 보고한다.
"""

from __future__ import annotations

import os

import pendulum
from airflow.providers.common.messaging.triggers.msg_queue import MessageQueueTrigger
from airflow.sdk import DAG, Asset, AssetWatcher, task
from p1_common import DEFAULT_ARGS, ecs_task_mapped
from p1_events import extract_objects, settle_dates

# 큐 URL 은 파싱 시점에 필요하다(트리거 생성). AWS 호출 없이 MWAA 사용자 설정(danbi.*)에서 읽는다
QUEUE_URL = os.environ.get(
    "AIRFLOW__DANBI__PG_SETTLEMENT_QUEUE_URL", "https://sqs.ap-northeast-2.amazonaws.com/000000000000/unset"
)
ARRIVALS = Asset(
    "danbi://landing/pg_settlement",
    watchers=[AssetWatcher(name="pg_settlement_sqs", trigger=MessageQueueTrigger(queue=QUEUE_URL))],
)


with DAG(
    dag_id="p1_pg_settlement",
    schedule=[ARRIVALS],
    start_date=pendulum.datetime(2026, 10, 1, tz="Asia/Seoul"),
    catchup=False,
    default_args=DEFAULT_ARGS,
    render_template_as_native_obj=True,
    params={"runbook": "pg-settlement.md"},
    tags=["p1", "recon", "event-driven"],
) as dag:

    @task
    def arrivals(**context) -> list[dict]:
        events = context["triggering_asset_events"].get(ARRIVALS, [])
        objs = extract_objects([e.extra for e in events])
        if not objs:
            raise ValueError("정산 파일 이벤트가 아니다")
        return objs

    @task
    def load_overrides(objs: list[dict]) -> list[dict]:
        cmds = []
        for o in objs:
            cmd = ["load-settlement", "--bucket", o["bucket"], "--key", o["key"]]
            if o.get("version_id"):
                cmd += ["--version-id", o["version_id"]]
            cmds.append({"containerOverrides": [{"name": "app", "command": cmd}]})
        return cmds

    @task
    def recon_overrides(objs: list[dict]) -> list[dict]:
        return [
            {"containerOverrides": [{"name": "app", "command": ["recon-pg", "--date", d]}]}
            for d in settle_dates(objs)
        ]

    objs = arrivals()
    loads = ecs_task_mapped("load_settlement", "recon_task_family").expand(overrides=load_overrides(objs))
    recons = ecs_task_mapped("recon_pg", "recon_task_family").expand(overrides=recon_overrides(objs))
    loads >> recons
