"""Bronze 수집 잡 (Managed Service for Apache Flink 1.20, PyFlink Table API).

    Kafka cdc.*      ─┐                               ┌─> glue.bronze.cdc_events
    Kafka events.*   ─┼─> Python UDTF(해석·PII 처리) ─┼─> glue.bronze.domain_events
    Kafka clicks.raw ─┘          │ 실패               └─> glue.bronze.clicks
                                 └──────────────────────> Kafka dlq.ingest (원문 + 오류)

정확히 한 번: Iceberg 싱크는 체크포인트 때 커밋한다. 체크포인트 간격 = Bronze 지연의 하한(NFR-01 p95 ≤ 30초 → 15초).
DLQ 는 최소 한 번(중복 가능)이다. 재처리 도구가 (topic, partition, offset) 로 중복을 거른다.
"""

from __future__ import annotations

import json
import os
import sys
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "site-packages"))

from pyflink.common import Row
from pyflink.table import DataTypes, EnvironmentSettings, TableEnvironment
from pyflink.table.udf import TableFunction, udtf

from p1.streaming.ingest.runtime import app_properties, build_decoder

RESULT_CDC = [
    ("ok", DataTypes.BOOLEAN()), ("source_db", DataTypes.STRING()), ("table_name", DataTypes.STRING()),
    ("op", DataTypes.STRING()), ("key_json", DataTypes.STRING()), ("before_json", DataTypes.STRING()),
    ("after_json", DataTypes.STRING()), ("change_seq", DataTypes.STRING()), ("txn_id", DataTypes.STRING()),
    ("source_ts", DataTypes.TIMESTAMP(3)), ("is_snapshot", DataTypes.BOOLEAN()), ("schema_id", DataTypes.INT()),
    ("error", DataTypes.STRING()),
]  # fmt: skip
CDC_FIELDS = [n for n, _ in RESULT_CDC[1:-1]]


class DecodeCdc(TableFunction):
    def open(self, function_context):
        self.decoder = build_decoder(app_properties())

    def eval(self, topic, key, value):
        try:
            row = self.decoder.cdc(topic, key, value)
            if row is None:
                return
            yield Row(True, *[row[f] for f in CDC_FIELDS], None)
        except Exception as e:  # 독약 메시지: 잡을 멈추지 않고 DLQ 로
            yield Row(False, *([None] * len(CDC_FIELDS)), f"{type(e).__name__}: {e}"[:2000])


RESULT_CLICK = [
    ("ok", DataTypes.BOOLEAN()), ("event_id", DataTypes.STRING()), ("event_type", DataTypes.STRING()),
    ("session_id", DataTypes.STRING()), ("member_id", DataTypes.STRING()), ("device_id", DataTypes.STRING()),
    ("platform", DataTypes.STRING()), ("page", DataTypes.STRING()), ("product_id", DataTypes.BIGINT()),
    ("search_query", DataTypes.STRING()), ("order_no", DataTypes.STRING()), ("user_agent", DataTypes.STRING()),
    ("event_time", DataTypes.TIMESTAMP(3)), ("sent_time", DataTypes.TIMESTAMP(3)), ("schema_id", DataTypes.INT()),
    ("error", DataTypes.STRING()),
]  # fmt: skip
CLICK_FIELDS = [n for n, _ in RESULT_CLICK[1:-1]]


class DecodeClick(TableFunction):
    def open(self, function_context):
        self.decoder = build_decoder(app_properties())

    def eval(self, value):
        try:
            row = self.decoder.click(value)
            yield Row(True, *[row[f] for f in CLICK_FIELDS], None)
        except Exception as e:
            yield Row(False, *([None] * len(CLICK_FIELDS)), f"{type(e).__name__}: {e}"[:2000])


RESULT_EVENT = [
    ("ok", DataTypes.BOOLEAN()), ("event_key", DataTypes.STRING()), ("event_id", DataTypes.STRING()),
    ("event_type", DataTypes.STRING()), ("payload_json", DataTypes.STRING()), ("occurred_at", DataTypes.TIMESTAMP(3)),
    ("schema_id", DataTypes.INT()), ("error", DataTypes.STRING()),
]  # fmt: skip
EVENT_FIELDS = [n for n, _ in RESULT_EVENT[1:-1]]


class DecodeEvent(TableFunction):
    def open(self, function_context):
        self.decoder = build_decoder(app_properties())

    def eval(self, topic, key, value, headers):
        try:
            row = self.decoder.domain_event(topic, key, value, dict(headers or {}))
            yield Row(True, *[row[f] for f in EVENT_FIELDS], None)
        except Exception as e:
            yield Row(False, *([None] * len(EVENT_FIELDS)), f"{type(e).__name__}: {e}"[:2000])


def _row_type(fields):
    return DataTypes.ROW([DataTypes.FIELD(n, t) for n, t in fields])


def kafka_options(p: dict[str, str], group: str) -> str:
    opts = {
        "connector": "kafka",
        "properties.bootstrap.servers": p["bootstrap"],
        "properties.group.id": group,
        "properties.security.protocol": "SASL_SSL",
        "properties.sasl.mechanism": "AWS_MSK_IAM",
        "properties.sasl.jaas.config": "software.amazon.msk.auth.iam.IAMLoginModule required;",
        "properties.sasl.client.callback.handler.class": "software.amazon.msk.auth.iam.IAMClientCallbackHandler",
        "scan.startup.mode": "group-offsets",
        "properties.auto.offset.reset": "earliest",
        "properties.partition.discovery.interval.ms": "60000",  # 새 CDC 토픽(테이블)을 재시작 없이 발견
    }
    return ",\n".join(f"'{k}' = '{v}'" for k, v in opts.items())


def main() -> None:
    p = app_properties()
    env = TableEnvironment.create(EnvironmentSettings.in_streaming_mode())
    conf = env.get_config()
    conf.set("table.local-time-zone", "UTC")
    conf.set("pipeline.name", "danbi-bronze-ingest")
    # 같은 UDTF 호출(정상 행 INSERT 와 DLQ INSERT)을 한 번만 실행하도록 서브플랜 재사용(기본값이지만 명시)
    conf.set("table.optimizer.reuse-sub-plan-enabled", "true")
    # Python UDF 워커 프로세스에도 의존성·코드가 보이도록 배포(zip 안의 site-packages, p1, danbi_common)
    root = os.path.dirname(os.path.abspath(__file__))
    for sub in ("site-packages", "p1", "libs/danbi_common"):
        path = os.path.join(root, *sub.split("/"))
        if os.path.exists(path):
            env.add_python_file(path)

    env.create_temporary_system_function("decode_cdc", udtf(DecodeCdc(), result_types=_row_type(RESULT_CDC)))
    env.create_temporary_system_function(
        "decode_click", udtf(DecodeClick(), result_types=_row_type(RESULT_CLICK))
    )
    env.create_temporary_system_function(
        "decode_event", udtf(DecodeEvent(), result_types=_row_type(RESULT_EVENT))
    )

    env.execute_sql(f"""
        CREATE CATALOG lake WITH (
          'type' = 'iceberg',
          'catalog-impl' = 'org.apache.iceberg.aws.glue.GlueCatalog',
          'io-impl' = 'org.apache.iceberg.aws.s3.S3FileIO',
          'warehouse' = 's3://{p["bronze_bucket"]}/'
        )""")

    raw_cols = """
        `topic` STRING METADATA VIRTUAL, `partition` INT METADATA VIRTUAL, `offset` BIGINT METADATA VIRTUAL,
        `headers` MAP<STRING, BYTES> METADATA VIRTUAL, `k` BYTES, `v` BYTES"""
    raw_fmt = "'key.format' = 'raw', 'key.fields' = 'k', 'value.format' = 'raw', 'value.fields-include' = 'EXCEPT_KEY'"
    env.execute_sql(f"CREATE TABLE src_cdc ({raw_cols}) WITH ({kafka_options(p, 'flink-bronze-cdc')}, "
                    f"'topic-pattern' = 'cdc\\..*', {raw_fmt})")  # fmt: skip
    env.execute_sql(f"CREATE TABLE src_events ({raw_cols}) WITH ({kafka_options(p, 'flink-bronze-events')}, "
                    f"'topic-pattern' = 'events\\..*', {raw_fmt})")  # fmt: skip
    env.execute_sql(f"CREATE TABLE src_clicks ({raw_cols}) WITH ({kafka_options(p, 'flink-bronze-clicks')}, "
                    f"'topic' = 'clicks.raw', {raw_fmt})")  # fmt: skip
    dlq_opts = kafka_options(p, "unused").replace("'scan.startup.mode' = 'group-offsets',\n", "")
    env.execute_sql(f"""
        CREATE TABLE dlq (
          `source_topic` STRING, `source_partition` INT, `source_offset` BIGINT, `error` STRING, `raw_value` BYTES
        ) WITH ({dlq_opts}, 'topic' = '{p.get("dlq_topic", "dlq.ingest")}', 'format' = 'json',
                'sink.delivery-guarantee' = 'at-least-once')""")

    s = env.create_statement_set()
    s.add_insert_sql("""
        INSERT INTO lake.bronze.cdc_events
        SELECT d.source_db, d.table_name, d.op, d.key_json, d.before_json, d.after_json, d.change_seq, d.txn_id,
               CAST(d.source_ts AS TIMESTAMP(6)), d.is_snapshot, d.schema_id,
               s.`topic`, s.`partition`, s.`offset`, CAST(CURRENT_TIMESTAMP AS TIMESTAMP(6))
        FROM src_cdc AS s, LATERAL TABLE(decode_cdc(s.`topic`, s.k, s.v)) AS d
        WHERE d.ok""")
    s.add_insert_sql("""
        INSERT INTO lake.bronze.domain_events
        SELECT s.`topic`, d.event_key, d.event_id, d.event_type, d.payload_json, CAST(d.occurred_at AS TIMESTAMP(6)),
               d.schema_id, s.`partition`, s.`offset`, CAST(CURRENT_TIMESTAMP AS TIMESTAMP(6))
        FROM src_events AS s, LATERAL TABLE(decode_event(s.`topic`, s.k, s.v, s.`headers`)) AS d
        WHERE d.ok""")
    s.add_insert_sql("""
        INSERT INTO lake.bronze.clicks
        SELECT d.event_id, d.event_type, d.session_id, d.member_id, d.device_id, d.platform, d.page, d.product_id,
               d.search_query, d.order_no, d.user_agent, CAST(d.event_time AS TIMESTAMP(6)),
               CAST(d.sent_time AS TIMESTAMP(6)), d.schema_id, s.`partition`, s.`offset`,
               CAST(CURRENT_TIMESTAMP AS TIMESTAMP(6))
        FROM src_clicks AS s, LATERAL TABLE(decode_click(s.v)) AS d
        WHERE d.ok""")
    for src, fn in (("src_cdc", "decode_cdc(s.`topic`, s.k, s.v)"),
                    ("src_events", "decode_event(s.`topic`, s.k, s.v, s.`headers`)"),
                    ("src_clicks", "decode_click(s.v)")):  # fmt: skip
        s.add_insert_sql(f"""
            INSERT INTO dlq
            SELECT s.`topic`, s.`partition`, s.`offset`, d.error, s.v
            FROM {src} AS s, LATERAL TABLE({fn}) AS d
            WHERE NOT d.ok""")
    s.execute()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print(json.dumps({"fatal": traceback.format_exc()}))
        raise
