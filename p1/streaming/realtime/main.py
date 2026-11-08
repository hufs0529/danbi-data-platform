"""실시간 잡 (Managed Flink 1.20, PyFlink DataStream + Table API).

    events.payment ─> 해석·PII(수집 잡과 같은 Decoder) ─> 이벤트 시간·워터마크(30초, 유휴 1분)
        ├─ key=전체 → 1분 텀블링(허용 지연 2분) → rt.sales_1m(Kafka) + lake.rt.sales_1m(Iceberg upsert)
        │                        └─ 너무 늦은 이벤트(사이드 출력) → lake.rt.late_events
        └─ key=card_token → KeyedProcessFunction(상태: 5분 이벤트 목록, 마지막 알림) → alerts.fraud(Kafka)

상태 있는 연산자에는 uid 를 고정한다. uid 가 바뀌면 스냅샷에서 상태를 복원하지 못해(F-09) 탐지 이력이 사라지고
중복 알림이 난다. 연산자 구조를 바꿀 때는 uid 를 유지하거나 상태 마이그레이션 절차를 따른다(runbook flink-upgrade).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "site-packages"))

from pyflink.common import Duration, Row, Time, Types, WatermarkStrategy  # noqa: E402
from pyflink.common.serialization import SimpleStringSchema  # noqa: E402
from pyflink.common.watermark_strategy import TimestampAssigner  # noqa: E402
from pyflink.datastream import OutputTag, StreamExecutionEnvironment  # noqa: E402
from pyflink.datastream.connectors.base import DeliveryGuarantee  # noqa: E402
from pyflink.datastream.connectors.kafka import KafkaRecordSerializationSchema, KafkaSink  # noqa: E402
from pyflink.datastream.functions import (  # noqa: E402
    AggregateFunction,
    KeyedProcessFunction,
    MapFunction,
    ProcessWindowFunction,
    RuntimeContext,
)
from pyflink.datastream.state import ListStateDescriptor, StateTtlConfig, ValueStateDescriptor  # noqa: E402
from pyflink.datastream.window import TumblingEventTimeWindows  # noqa: E402
from pyflink.table import StreamTableEnvironment  # noqa: E402

from p1.streaming.ingest.main import kafka_options  # noqa: E402
from p1.streaming.ingest.runtime import app_properties, build_decoder  # noqa: E402
from p1.streaming.realtime import logic  # noqa: E402

LATE = OutputTag("late-events", Types.PICKLED_BYTE_ARRAY())


class Decode(MapFunction):
    def open(self, runtime_context: RuntimeContext):
        self.decoder = build_decoder(app_properties())

    def map(self, row):
        try:
            ev = self.decoder.domain_event(row["topic"], row["k"], row["v"], dict(row["headers"] or {}))
            return logic.parse_event(ev)
        except Exception:  # 해석 실패는 수집 잡이 DLQ 로 보낸다. 실시간 잡은 건너뛴다
            return None


class EventTime(TimestampAssigner):
    def extract_timestamp(self, value, record_timestamp):
        return value.epoch_ms


class SalesAgg(AggregateFunction):
    def create_accumulator(self):
        return logic.SalesAcc()

    def add(self, value, accumulator):
        return accumulator.add(value)

    def get_result(self, accumulator):
        return accumulator

    def merge(self, a, b):
        return a.merge(b)


class SalesWindow(ProcessWindowFunction):
    """허용 지연 안에서 같은 창이 다시 발화하면 revision 을 올린다(소비자는 window_start 로 덮어쓴다)."""

    def open(self, runtime_context: RuntimeContext):
        self.rev = runtime_context.get_state(ValueStateDescriptor("revision", Types.INT()))

    def process(self, key, context, elements):
        acc = next(iter(elements))
        start = dt.datetime.fromtimestamp(context.window().start / 1000, dt.UTC).replace(tzinfo=None)
        r = (self.rev.value() or 0) + 1
        self.rev.update(r)
        row = logic.sales_row(start, acc, r, dt.datetime.now(dt.UTC).replace(tzinfo=None))
        yield row

    def clear(self, context):
        self.rev.clear()


class FraudDetector(KeyedProcessFunction):
    def open(self, runtime_context: RuntimeContext):
        ttl = StateTtlConfig.new_builder(Time.minutes(15)).set_update_type(
            StateTtlConfig.UpdateType.OnCreateAndWrite).build()  # fmt: skip
        entries = ListStateDescriptor("entries", Types.PICKLED_BYTE_ARRAY())
        entries.enable_time_to_live(ttl)
        last = ValueStateDescriptor("last_alert_ms", Types.LONG())
        last.enable_time_to_live(ttl)
        self.entries = runtime_context.get_list_state(entries)
        self.last = runtime_context.get_state(last)

    def process_element(self, value, ctx):
        state = logic.FraudState(list(self.entries.get() or []), self.last.value())
        now_ms = int(dt.datetime.now(dt.UTC).timestamp() * 1000)
        state, alert = logic.detect(state, value, now_ms)
        self.entries.update(state.entries)
        if state.last_alert_ms is not None:
            self.last.update(state.last_alert_ms)
        if alert:
            yield json.dumps(alert, ensure_ascii=False)


def _kafka_sink(p: dict, topic: str) -> KafkaSink:
    b = (KafkaSink.builder().set_bootstrap_servers(p["bootstrap"])
         .set_record_serializer(KafkaRecordSerializationSchema.builder().set_topic(topic)
                                .set_value_serialization_schema(SimpleStringSchema()).build())
         .set_delivery_guarantee(DeliveryGuarantee.AT_LEAST_ONCE))  # fmt: skip
    for k, v in {"security.protocol": "SASL_SSL", "sasl.mechanism": "AWS_MSK_IAM",
                 "sasl.jaas.config": "software.amazon.msk.auth.iam.IAMLoginModule required;",
                 "sasl.client.callback.handler.class": "software.amazon.msk.auth.iam.IAMClientCallbackHandler"}.items():  # fmt: skip
        b = b.set_property(k, v)
    return b.build()


SALES_TYPE = Types.ROW_NAMED(
    ["window_start", "window_end", "approved_amount", "captured_amount", "canceled_amount", "net_amount",
     "payments", "null_amounts", "revision", "emitted_at"],
    [Types.SQL_TIMESTAMP(), Types.SQL_TIMESTAMP(), Types.BIG_DEC(), Types.BIG_DEC(), Types.BIG_DEC(), Types.BIG_DEC(),
     Types.LONG(), Types.LONG(), Types.INT(), Types.SQL_TIMESTAMP()],
)  # fmt: skip


def main() -> None:
    p = app_properties()
    env = StreamExecutionEnvironment.get_execution_environment()
    t_env = StreamTableEnvironment.create(env)
    t_env.get_config().set("table.local-time-zone", "UTC")
    t_env.get_config().set("pipeline.name", "danbi-realtime")
    root = os.path.dirname(os.path.abspath(__file__))
    for sub in ("site-packages", "p1", "libs/danbi_common"):
        path = os.path.join(root, *sub.split("/"))
        if os.path.exists(path):
            env.add_python_file(path)

    t_env.execute_sql(f"""CREATE CATALOG lake WITH ('type'='iceberg',
        'catalog-impl'='org.apache.iceberg.aws.glue.GlueCatalog', 'io-impl'='org.apache.iceberg.aws.s3.S3FileIO',
        'warehouse'='s3://{p["gold_bucket"]}/_rt/')""")
    # Athena 는 Iceberg 식별자 필드(기본키)를 만들 수 없어, upsert 대상 테이블은 Flink 가 직접 만든다
    t_env.execute_sql("""CREATE TABLE IF NOT EXISTS lake.rt.sales_1m (
        window_start TIMESTAMP(6), window_end TIMESTAMP(6), approved_amount DECIMAL(18,0),
        captured_amount DECIMAL(18,0), canceled_amount DECIMAL(18,0), net_amount DECIMAL(18,0),
        payments BIGINT, null_amounts BIGINT, revision INT, emitted_at TIMESTAMP(6),
        PRIMARY KEY (window_start) NOT ENFORCED
      ) WITH ('format-version'='2', 'write.upsert.enabled'='true')""")
    t_env.execute_sql("""CREATE TABLE IF NOT EXISTS lake.rt.late_events (
        payment_key STRING, to_status STRING, amount DECIMAL(18,0), occurred_at TIMESTAMP(6), seen_at TIMESTAMP(6)
      ) WITH ('format-version'='2')""")

    t_env.execute_sql(f"""CREATE TABLE src_events (
        `topic` STRING METADATA VIRTUAL, `headers` MAP<STRING, BYTES> METADATA VIRTUAL, `k` BYTES, `v` BYTES
      ) WITH ({kafka_options(p, "flink-realtime")}, 'topic' = 'events.payment',
        'key.format' = 'raw', 'key.fields' = 'k', 'value.format' = 'raw', 'value.fields-include' = 'EXCEPT_KEY')""")

    raw = t_env.to_data_stream(t_env.from_path("src_events"))
    events = (raw.map(Decode(), output_type=Types.PICKLED_BYTE_ARRAY()).uid("decode-v1")
              .filter(lambda e: e is not None).uid("filter-v1")
              .assign_timestamps_and_watermarks(
                  WatermarkStrategy.for_bounded_out_of_orderness(Duration.of_seconds(30))
                  .with_timestamp_assigner(EventTime()).with_idleness(Duration.of_minutes(1))).uid("wm-v1"))  # fmt: skip

    sales = (events.key_by(lambda e: "all", key_type=Types.STRING())
             .window(TumblingEventTimeWindows.of(Time.minutes(1)))
             .allowed_lateness(120_000)
             .side_output_late_data(LATE)
             .aggregate(SalesAgg(), SalesWindow(), accumulator_type=Types.PICKLED_BYTE_ARRAY(),
                        output_type=Types.PICKLED_BYTE_ARRAY())
             .uid("sales-1m-v1"))  # fmt: skip
    sales.map(lambda r: json.dumps(r, ensure_ascii=False, default=str), output_type=Types.STRING()).sink_to(
        _kafka_sink(p, "rt.sales_1m")
    ).uid("sink-sales-kafka-v1")
    sales_rows = sales.map(
        lambda r: Row(*[r[n] for n in SALES_TYPE.get_field_names()]), output_type=SALES_TYPE
    )
    late = sales.get_side_output(LATE).map(
        lambda e: Row(e.payment_key, e.to_status, e.amount, e.occurred_at, dt.datetime.now(dt.UTC).replace(tzinfo=None)),
        output_type=Types.ROW_NAMED(["payment_key", "to_status", "amount", "occurred_at", "seen_at"],
                                    [Types.STRING(), Types.STRING(), Types.BIG_DEC(), Types.SQL_TIMESTAMP(), Types.SQL_TIMESTAMP()]))  # fmt: skip

    (events.filter(lambda e: e.to_status == "APPROVED" and e.card_token is not None)
     .key_by(lambda e: e.card_token, key_type=Types.STRING())
     .process(FraudDetector(), output_type=Types.STRING()).uid("fraud-detector-v1")
     .sink_to(_kafka_sink(p, "alerts.fraud")).uid("sink-fraud-kafka-v1"))  # fmt: skip

    stmts = t_env.create_statement_set()
    stmts.add_insert("lake.rt.sales_1m", t_env.from_data_stream(sales_rows))
    stmts.add_insert("lake.rt.late_events", t_env.from_data_stream(late))
    stmts.attach_as_datastream()
    env.execute("danbi-realtime")


if __name__ == "__main__":
    main()
