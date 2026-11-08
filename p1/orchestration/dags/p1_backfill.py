"""수동 백필·재처리 (S-C2: 할인 계산 버그로 3일치 Gold 가 틀렸다).

절차: 영향 범위 확인(Marquez 계보: 하류 데이터셋 목록) → Silver 구간 재처리(멱등) → Gold 재빌드(gold_audit, 테스트)
→ 사람 확인 후 게시(publish=true) → 소비자 공지(템플릿). 게시 전까지 대시보드는 기존 값을 계속 보여준다(중단 0).
"""

from __future__ import annotations

import json
import urllib.request

import pendulum
from airflow.sdk import DAG, Param, task
from p1_common import CFG, DEFAULT_ARGS, ecs_task, spark_job

RUN = "{{ run_id | replace(':', '-') | replace('+', '-') }}"

with DAG(
    dag_id="p1_backfill",
    schedule=None,
    start_date=pendulum.datetime(2026, 10, 1, tz="Asia/Seoul"),
    catchup=False,
    max_active_runs=1,
    default_args={**DEFAULT_ARGS, "retries": 0},
    render_template_as_native_obj=True,
    params={
        "tables": Param("silver.orders,silver.order_items", type="string"),
        "start": Param("2026-10-27T00:00:00", type="string", description="Bronze ingest_ts (UTC)"),
        "end": Param("2026-10-30T00:00:00", type="string"),
        "publish": Param(False, type="boolean", description="검증 결과를 사람이 확인한 뒤 true 로 다시 실행"),
        "runbook": "backfill.md",
    },
    tags=["p1", "backfill", "manual"],
) as dag:

    @task
    def impact(**context) -> list[str]:
        """Marquez 계보 API 로 하류 데이터셋을 찾는다(공지 대상·재빌드 범위)."""
        from airflow.sdk import Variable

        url = Variable.get("p1_config", deserialize_json=True)["marquez_url"]
        downstream = set()
        for t in context["params"]["tables"].split(","):
            node = f"dataset:danbi-dev:{t.strip()}"
            try:
                with urllib.request.urlopen(f"{url}/api/v1/lineage?nodeId={node}&depth=5", timeout=10) as r:  # noqa: S310
                    graph = json.loads(r.read())
                downstream |= {n["id"] for n in graph.get("graph", []) if n.get("type") == "DATASET"}
            except OSError as e:
                print(f"계보 조회 실패(수동 확인 필요): {e}")
        print("영향 받는 데이터셋:", sorted(downstream))
        return sorted(downstream)

    reprocess = spark_job("silver_reprocess", "silver_merge",
                          ["--tables", "{{ params.tables }}", "--from", "{{ params.start }}", "--to", "{{ params.end }}"])  # fmt: skip
    rebuild = ecs_task("dbt_rebuild_audit", "dbt_task_family", ["build", "--select", "tag:publish"],
                       env={"DBT_RUN_ID": RUN, "OPENLINEAGE_URL": "{{ " + CFG + ".marquez_url }}"})  # fmt: skip

    @task.short_circuit
    def approved(**context) -> bool:
        return bool(context["params"]["publish"])

    publish = spark_job("publish_gold", "publish_gold",
                        ["--run-results", "s3://{{ " + CFG + ".artifacts_bucket }}/dbt/runs/" + RUN + "/run_results.json"])  # fmt: skip

    impact() >> reprocess >> rebuild >> approved() >> publish
