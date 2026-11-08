"""P1 DAG 공통: 설정(Airflow Variable p1_config ← SSM), ECS·EMR 작업 생성기, 실패 알림.

원칙
- 파싱 시점에 AWS 를 호출하지 않는다(스케줄러가 파일을 자주 다시 읽는다). 값은 템플릿으로 실행 시점에 푼다.
- 모든 DAG 는 render_template_as_native_obj=True: 템플릿 결과를 리스트·딕셔너리 그대로 넘긴다(서브넷 목록 등).
- 실패 알림에는 런북 링크를 붙인다. 새벽 3시에 받은 사람이 런북만 보고 판단할 수 있어야 한다.
"""

from __future__ import annotations

import json

from airflow.providers.amazon.aws.operators.ecs import EcsRunTaskOperator
from airflow.providers.amazon.aws.operators.emr import EmrServerlessStartJobOperator

CFG = "var.json.p1_config"
RUNBOOK = "https://github.com/hufs0529/danbi-data-platform/blob/main/docs/runbooks"


def cfg(key: str) -> str:
    return "{{ " + f"{CFG}.{key}" + " }}"


def notify_failure(context) -> None:
    import boto3
    from airflow.sdk import Variable

    c = Variable.get("p1_config", deserialize_json=True)
    ti = context["ti"]
    runbook = context["dag"].params.get("runbook", "README.md") if context.get("dag") else "README.md"
    boto3.client("sns", region_name=c["region"]).publish(
        TopicArn=c["alerts_topic_arn"],
        Subject=f"[P1][FAIL] {ti.dag_id}.{ti.task_id}"[:100],
        Message=json.dumps(
            {
                "dag": ti.dag_id,
                "task": ti.task_id,
                "run_id": context["run_id"],
                "try": ti.try_number,
                "log": ti.log_url,
                "runbook": f"{RUNBOOK}/{runbook}",
            },
            ensure_ascii=False,
            indent=1,
        ),  # fmt: skip
    )


DEFAULT_ARGS = {
    "owner": "data-platform",
    "retries": 2,
    "retry_exponential_backoff": True,
    "on_failure_callback": notify_failure,
}


def _ecs_common(task_id: str, family_key: str, sg_key: str) -> dict:
    return {
        "task_id": task_id,
        "cluster": cfg("ecs_cluster"),
        "task_definition": cfg(family_key),
        "launch_type": "FARGATE",
        "network_configuration": {
            "awsvpcConfiguration": {
                "subnets": "{{ " + CFG + ".app_subnets }}",
                "securityGroups": ["{{ " + CFG + "." + sg_key + " }}"],
                "assignPublicIp": "DISABLED",
            }
        },  # fmt: skip
        "awslogs_group": cfg("ecs_log_group"),
        "awslogs_stream_prefix": "{{ " + CFG + "." + family_key + " }}/app",
        "awslogs_region": cfg("region"),
        "do_xcom_push": True,  # 컨테이너 마지막 로그 줄(JSON 결과)을 XCom 으로
    }


def ecs_task(task_id: str, family_key: str, command: list[str] | str, sg_key: str = "db_clients_sg",
             env: dict[str, str] | None = None, **kw) -> EcsRunTaskOperator:  # fmt: skip
    container: dict = {"name": "app", "command": command}
    if env:
        container["environment"] = [{"name": k, "value": v} for k, v in env.items()]
    return EcsRunTaskOperator(
        overrides={"containerOverrides": [container]}, **_ecs_common(task_id, family_key, sg_key), **kw
    )


def ecs_task_mapped(task_id: str, family_key: str, sg_key: str = "db_clients_sg", **kw):
    """동적 매핑용: .expand(overrides=[...]) 로 파일마다 태스크 하나."""
    return EcsRunTaskOperator.partial(**_ecs_common(task_id, family_key, sg_key), **kw)


def spark_job(
    task_id: str, job: str, args: list[str] | str | None = None, **kw
) -> EmrServerlessStartJobOperator:
    return EmrServerlessStartJobOperator(
        task_id=task_id,
        application_id=cfg("emr_app_id"),
        execution_role_arn=cfg("emr_job_role_arn"),
        name=f"{job}-{{{{ ts_nodash }}}}",
        job_driver={
            "sparkSubmit": {
                "entryPoint": "{{ " + CFG + ".spark_entry_prefix }}/" + job + ".py",
                "entryPointArguments": args or [],
                "sparkSubmitParameters": cfg("spark_submit_params"),
            }
        },  # fmt: skip
        configuration_overrides={
            "monitoringConfiguration": {"s3MonitoringConfiguration": {"logUri": cfg("emr_log_uri")}}
        },
        wait_for_completion=True,
        deferrable=True,  # 기다리는 동안 워커 슬롯을 비운다(mw1.micro 동시 태스크 3)
        **kw,
    )


def athena_scalar(sql: str) -> str | None:
    import time

    import boto3
    from airflow.sdk import Variable

    c = Variable.get("p1_config", deserialize_json=True)
    a = boto3.client("athena", region_name=c["region"])
    qid = a.start_query_execution(QueryString=sql, WorkGroup=c["athena_workgroup"])["QueryExecutionId"]
    while True:
        st = a.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"]["State"]
        if st in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        time.sleep(2)
    if st != "SUCCEEDED":
        raise RuntimeError(f"athena {st}: {sql}")
    rows = a.get_query_results(QueryExecutionId=qid)["ResultSet"]["Rows"]
    return rows[1]["Data"][0].get("VarCharValue") if len(rows) > 1 else None
