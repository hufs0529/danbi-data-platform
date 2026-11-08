"""EMR Serverless Spark 잡 공통: 세션, 워터마크, 제출 매개변수."""

from __future__ import annotations

import datetime as dt
import json
import uuid

CATALOG = "glue"


# Spark 기본 설정의 단일 원본은 p1/batch/spark-defaults.json 이다(Terraform 도 같은 파일을 읽어 SSM 에 제출 매개변수를 쓴다).
# EMR 7.x 는 Iceberg 런타임 jar 를 이미지에 포함한다. 카탈로그는 Glue, 파일 IO 는 S3FileIO.
# merge-on-read: MERGE 가 삭제 파일을 써서 쓰기 증폭을 줄인다. 대신 유지보수 잡의 컴팩션이 필요하다.
def _load_defaults() -> dict[str, str]:
    from importlib import resources

    return json.loads(resources.files("p1.batch").joinpath("spark-defaults.json").read_text(encoding="utf-8"))


SPARK_CONF = _load_defaults()


def submit_params(
    entry: str, warehouse: str, extra: dict[str, str] | None = None, py_files: str | None = None
) -> str:
    """EMR Serverless StartJobRun 의 sparkSubmitParameters 문자열."""
    conf = {**SPARK_CONF, f"spark.sql.catalog.{CATALOG}.warehouse": warehouse, **(extra or {})}
    parts = [f"--conf {k}={v}" for k, v in conf.items()]
    if py_files:
        parts.append(f"--py-files {py_files}")
    return " ".join(parts)


def session(app: str):
    from pyspark.sql import SparkSession

    b = SparkSession.builder.appName(app)
    return b.getOrCreate()


def new_run_id(prefix: str) -> str:
    return f"{prefix}-{dt.datetime.now(dt.UTC):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"


def read_watermark(spark, pipeline: str, target: str) -> dt.datetime | None:
    rows = spark.sql(
        f"SELECT last_ingest_ts FROM {CATALOG}.ops.pipeline_watermarks "
        f"WHERE pipeline = '{pipeline}' AND target_table = '{target}'"
    ).collect()
    return rows[0][0] if rows else None


def write_watermark(spark, pipeline: str, source: str, target: str, ts: dt.datetime, run_id: str) -> None:
    spark.sql(f"""
MERGE INTO {CATALOG}.ops.pipeline_watermarks t
USING (SELECT '{pipeline}' AS pipeline, '{source}' AS source_table, '{target}' AS target_table,
              CAST(NULL AS BIGINT) AS last_snapshot_id, TIMESTAMP '{ts:%Y-%m-%d %H:%M:%S.%f}' AS last_ingest_ts,
              '{run_id}' AS run_id, current_timestamp() AS updated_at) s
ON t.pipeline = s.pipeline AND t.target_table = s.target_table
WHEN MATCHED THEN UPDATE SET *
WHEN NOT MATCHED THEN INSERT *""")
