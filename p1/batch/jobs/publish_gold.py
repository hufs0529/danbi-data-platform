"""WAP 의 Publish 단계: gold_audit(검증 통과) → gold 원자적 교체 (EMR Serverless, Spark).

    spark-submit publish_gold.py --run-results s3://<artifacts>/dbt/runs/<run_id>/run_results.json
    spark-submit publish_gold.py --models fct_payments,mart_daily_sales --rollback   # 직전 스냅샷으로 되돌리기

- dbt build(모델 + 테스트)가 모두 성공한 실행의 run_results 만 받는다. 실패가 하나라도 있으면 아무것도 게시하지 않는다.
- INSERT OVERWRITE 한 번 = Iceberg 커밋 한 번. 읽는 쪽은 이전 스냅샷 또는 새 스냅샷만 본다(중간 상태 없음).
- 새 컬럼은 ADD COLUMNS 후 게시, 타입 변경·컬럼 삭제는 거부(소비자 보호).
- 직전 스냅샷 ID 를 ops.publish_log 에 남겨 rollback_to_snapshot 으로 되돌린다.
"""

from __future__ import annotations

import argparse
import json

from p1.batch.jobs.common import CATALOG, new_run_id, session

AUDIT_DB, GOLD_DB = "gold_audit", "gold"


def gate(run_results: dict) -> tuple[list[str], list[str]]:
    """(게시할 모델, 실패 목록). 실패가 있으면 게시 목록은 비운다."""
    failures, models = [], []
    for r in run_results.get("results", []):
        uid, status = r["unique_id"], r["status"]
        if status in ("error", "fail", "runtime error"):
            failures.append(f"{uid}: {status}")
        elif uid.startswith("model.") and status == "success":
            models.append(uid.split(".")[-1])
    return ([] if failures else sorted(models)), failures


def schema_diff(
    audit: list[tuple[str, str]], gold: list[tuple[str, str]] | None
) -> tuple[list[tuple[str, str]], list[str]]:
    if gold is None:
        return [], []
    g = dict(gold)
    added = [(n, t) for n, t in audit if n not in g]
    errors = [f"{n}: {g[n]} → {t}" for n, t in audit if n in g and g[n] != t]
    errors += [f"{n}: 삭제됨" for n in g if n not in dict(audit)]
    return added, errors


def publish_sql(model: str, exists: bool) -> str:
    if not exists:
        return f"CREATE TABLE {CATALOG}.{GOLD_DB}.{model} USING iceberg AS SELECT * FROM {CATALOG}.{AUDIT_DB}.{model}"
    return f"INSERT OVERWRITE {CATALOG}.{GOLD_DB}.{model} SELECT * FROM {CATALOG}.{AUDIT_DB}.{model}"


def _columns(spark, table: str) -> list[tuple[str, str]] | None:
    if not spark.catalog.tableExists(table):
        return None
    return [(f.name, f.dataType.simpleString()) for f in spark.table(table).schema.fields]


def _snapshot(spark, table: str) -> int | None:
    rows = spark.sql(
        f"SELECT snapshot_id FROM {table}.snapshots ORDER BY committed_at DESC LIMIT 1"
    ).collect()
    return rows[0][0] if rows else None


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--run-results")
    p.add_argument("--models")
    p.add_argument("--rollback", action="store_true")
    args = p.parse_args(argv)
    spark = session("publish-gold")
    # 파티션 덮어쓰기가 아니라 테이블 전체 교체(마트는 매일 전체 재계산)
    spark.conf.set("spark.sql.sources.partitionOverwriteMode", "static")
    run_id = new_run_id("publish")

    if args.rollback:
        for m in args.models.split(","):
            prev = spark.sql(f"SELECT previous_snapshot_id FROM {CATALOG}.ops.publish_log WHERE model = '{m}' "
                             "ORDER BY published_at DESC LIMIT 1").collect()  # fmt: skip
            if prev and prev[0][0]:
                spark.sql(f"CALL {CATALOG}.system.rollback_to_snapshot('{GOLD_DB}.{m}', {prev[0][0]})")
                print(f"rolled back gold.{m} → {prev[0][0]}")
        return

    if args.run_results:
        text = spark.sparkContext.wholeTextFiles(args.run_results).collect()[0][1]
        results = json.loads(text)
        models, failures = gate(results)
        invocation = results.get("metadata", {}).get("invocation_id", "")
        if failures:
            raise SystemExit("감사 실패 → 게시 중단:\n" + "\n".join(failures))
    else:
        models, invocation = args.models.split(","), ""

    for m in models:
        audit_t, gold_t = f"{CATALOG}.{AUDIT_DB}.{m}", f"{CATALOG}.{GOLD_DB}.{m}"
        if _columns(spark, audit_t) is None:
            continue  # ephemeral·시맨틱 보조 모델
        added, errors = schema_diff(_columns(spark, audit_t), _columns(spark, gold_t))
        if errors:
            raise SystemExit(f"{m}: 비호환 스키마 변경 → 게시 거부 {errors}")
        exists = _columns(spark, gold_t) is not None
        prev = _snapshot(spark, gold_t) if exists else None
        for n, t in added:
            spark.sql(f"ALTER TABLE {gold_t} ADD COLUMNS ({n} {t})")
        spark.sql(publish_sql(m, exists))
        new = _snapshot(spark, gold_t)
        rows = spark.table(gold_t).count()
        spark.sql(f"""INSERT INTO {CATALOG}.ops.publish_log VALUES ('{m}', '{run_id}', '{invocation}',
            {_snapshot(spark, audit_t) or "NULL"}, {new or "NULL"}, {prev or "NULL"}, {rows},
            '{",".join(n for n, _ in added)}', current_timestamp())""")
        print(f"published gold.{m}: snapshot {prev} → {new}, rows={rows}")


if __name__ == "__main__":
    main()
