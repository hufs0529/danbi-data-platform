"""Bronze → Silver 증분 MERGE (EMR Serverless, 5~15분 주기, ADR-06).

    spark-submit silver_merge.py --tables all --overlap-minutes 10
    spark-submit silver_merge.py --tables silver.orders --from 2026-10-27T00:00:00 --to 2026-10-30T00:00:00   # 백필(S-C2)

증분 기준은 Bronze 의 ingest_ts 워터마크다. Iceberg 커밋은 체크포인트 때 일어나므로, 이미 지난 ingest_ts 의
행이 나중에 보일 수 있다 → 겹침 창(overlap)만큼 다시 읽고, MERGE 의 _src_seq 비교가 중복을 흡수한다.
같은 구간을 몇 번 돌려도 결과가 같다(FR-09 멱등 재처리).
"""

from __future__ import annotations

import argparse
import datetime as dt

from contracts.registry import Contract, load_all
from p1.batch import transforms as T
from p1.batch.jobs.common import CATALOG, new_run_id, read_watermark, session, write_watermark

PIPELINE = "silver_merge"


def bronze_increment(spark, c: Contract, start: dt.datetime | None, end: dt.datetime | None):
    from pyspark.sql import functions as F

    src = c.source
    if "db" in src:
        df = spark.table(f"{CATALOG}.bronze.cdc_events").where(
            (F.col("source_db") == src["db"]) & (F.col("table_name") == src["table"])
        )
    elif src.get("topic") == "clicks.raw":
        df = spark.table(f"{CATALOG}.bronze.clicks")
    elif src.get("topic", "").startswith("events."):
        df = spark.table(f"{CATALOG}.bronze.domain_events").where(F.col("topic") == src["topic"])
    else:
        return None
    if start is not None:
        df = df.where(F.col("ingest_ts") > F.lit(start))
    if end is not None:
        df = df.where(F.col("ingest_ts") <= F.lit(end))
    return df


def events_to_rows(df, c: Contract):
    """Outbox(payload_json) 또는 클릭(타입 있는 컬럼) → 계약 컬럼. 중복은 가장 먼저 수집된 것을 남긴다."""
    from pyspark.sql import Window
    from pyspark.sql import functions as F
    from pyspark.sql.types import MapType, StringType

    if "payload_json" in df.columns:
        m = F.map_concat(
            F.from_json("payload_json", MapType(StringType(), StringType())),
            F.create_map(F.lit("event_id"), F.col("event_id"), F.lit("event_type"), F.col("event_type")),
        )
        cols = [T.cast_expr(m.getItem(col.bronze_field), col.type, col.transform).alias(col.name)
                for col in c.columns if col.source]  # fmt: skip
    else:
        cols = [F.col(col.source).cast(col.type).alias(col.name) for col in c.columns if col.source]
    out = df.select(*cols, "ingest_ts")
    for col in c.columns:
        if col.derive:
            out = out.withColumn(col.name, T.derive_expr(col.derive).cast(col.type))
    w = Window.partitionBy(*c.grain).orderBy("ingest_ts")
    return out.withColumn("_rn", F.row_number().over(w)).where("_rn = 1").drop("_rn", "ingest_ts")


def record_drift(spark, c: Contract, fields: list[str], run_id: str) -> None:
    for f in fields:
        spark.sql(f"""
MERGE INTO {CATALOG}.ops.schema_drift t
USING (SELECT '{c.dataset}' AS dataset, '{f}' AS column_name) s
ON t.dataset = s.dataset AND t.column_name = s.column_name
WHEN NOT MATCHED THEN INSERT (dataset, column_name, observed_type, first_seen_at, run_id, status)
VALUES (s.dataset, s.column_name, 'string', current_timestamp(), '{run_id}', 'PENDING')""")
        print(f"[drift] {c.dataset}.{f}: 계약 밖 필드 → ops.schema_drift (분류 대기)")


def process(spark, c: Contract, start, end, run_id: str) -> int:
    from pyspark.sql import functions as F

    inc = bronze_increment(spark, c, start, end)
    if inc is None:
        return 0
    inc = inc.persist()
    n = inc.count()
    if n == 0:
        inc.unpersist()
        return 0
    view = f"_src_{c.table}"
    if c.update in ("merge", "scd2") and "after_json" in inc.columns:
        drift = T.unknown_fields(inc, c)
        extra: tuple[str, ...] = ()
        if drift and c.update == "merge":
            record_drift(spark, c, drift, run_id)
            have = set(spark.table(f"{CATALOG}.{c.dataset}").columns)
            for f in drift:
                if f not in have:  # S-C1: 호환 변경(컬럼 추가)은 다음 주기에 Silver 에 나타난다
                    spark.sql(f"ALTER TABLE {CATALOG}.{c.dataset} ADD COLUMNS ({f} string)")
            extra = tuple(drift)
        rows = T.cdc_to_rows(inc, c, extra)
        if c.update == "merge":
            T.latest_per_key(rows, list(c.grain)).createOrReplaceTempView(view)
            spark.sql(T.merge_sql(c, view, run_id, extra=extra))
        else:
            current = spark.table(f"{CATALOG}.{c.dataset}").where("is_current")
            T.scd2_versions(rows, current, c).createOrReplaceTempView(view)
            spark.sql(T.scd2_merge_sql(c, view, run_id))
    else:
        events_to_rows(inc, c).createOrReplaceTempView(view)
        spark.sql(T.append_dedup_sql(c, view, run_id))
    max_ts = inc.agg(F.max("ingest_ts")).collect()[0][0]
    inc.unpersist()
    if end is None and max_ts is not None:  # 백필은 워터마크를 움직이지 않는다
        write_watermark(spark, PIPELINE, str(c.source), c.dataset, max_ts, run_id)
    return n


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--tables", default="all", help="all 또는 쉼표로 구분한 silver.<table>")
    p.add_argument("--overlap-minutes", type=int, default=10)
    p.add_argument("--from", dest="start", help="백필 시작(ingest_ts, UTC ISO)")
    p.add_argument("--to", dest="end", help="백필 끝")
    args = p.parse_args(argv)

    spark = session("silver-merge")
    run_id = new_run_id("silver")
    wanted = None if args.tables == "all" else set(args.tables.split(","))
    targets = [c for c in load_all() if c.layer == "silver" and c.update in ("merge", "scd2", "append")
               and (wanted is None or c.dataset in wanted)]  # fmt: skip
    for c in targets:
        if args.start:
            start = dt.datetime.fromisoformat(args.start)
            end = dt.datetime.fromisoformat(args.end) if args.end else None
        else:
            wm = read_watermark(spark, PIPELINE, c.dataset)
            start = wm - dt.timedelta(minutes=args.overlap_minutes) if wm else None
            end = None
        n = process(spark, c, start, end, run_id)
        print(f"[{run_id}] {c.dataset}: {n} bronze rows")


if __name__ == "__main__":
    main()
