"""Iceberg 유지보수 (매일 새벽, F-07 작은 파일 폭증 대응).

1. 보존 기간이 지난 Bronze 행 삭제(90일) — S3 수명 주기가 아니라 테이블 연산으로
2. rewrite_data_files: 작은 파일·삭제 파일(merge-on-read) 합치기
3. expire_snapshots: 오래된 스냅샷 제거(시간 여행 범위 = 7일). 파기(P1-13)는 이 단계를 거쳐야 물리적으로 완료된다
4. remove_orphan_files: 실패한 커밋이 남긴 파일 정리(3일 이상 지난 것만 — 진행 중 커밋 보호)
5. rewrite_manifests: 매니페스트 정리(계획 시간 단축)
"""

from __future__ import annotations

import argparse
import datetime as dt

from p1.batch.jobs.common import CATALOG, session

BRONZE_RETENTION_DAYS = {"bronze.cdc_events": 90, "bronze.domain_events": 90, "bronze.clicks": 90}
TARGET_FILE_BYTES = 128 * 1024 * 1024


def tables(spark) -> list[str]:
    out = []
    for db in ("bronze", "silver", "gold"):
        out += [f"{db}.{r.tableName}" for r in spark.sql(f"SHOW TABLES IN {CATALOG}.{db}").collect()]
    return out


def maintenance_sql(table: str, now: dt.datetime, snapshot_days: int = 7, orphan_days: int = 3) -> list[str]:
    older = (now - dt.timedelta(days=snapshot_days)).strftime("%Y-%m-%d %H:%M:%S")
    orphan = (now - dt.timedelta(days=orphan_days)).strftime("%Y-%m-%d %H:%M:%S")
    stmts = []
    if table in BRONZE_RETENTION_DAYS:
        cutoff = (now - dt.timedelta(days=BRONZE_RETENTION_DAYS[table])).strftime("%Y-%m-%d %H:%M:%S")
        stmts.append(f"DELETE FROM {CATALOG}.{table} WHERE ingest_ts < TIMESTAMP '{cutoff}'")
    stmts += [
        f"CALL {CATALOG}.system.rewrite_data_files(table => '{table}', strategy => 'binpack', "
        f"options => map('min-input-files', '5', 'target-file-size-bytes', '{TARGET_FILE_BYTES}', "
        f"'delete-file-threshold', '10', 'partial-progress.enabled', 'true'))",
        f"CALL {CATALOG}.system.expire_snapshots(table => '{table}', older_than => TIMESTAMP '{older}', "
        f"retain_last => 20)",
        f"CALL {CATALOG}.system.remove_orphan_files(table => '{table}', older_than => TIMESTAMP '{orphan}')",
        f"CALL {CATALOG}.system.rewrite_manifests(table => '{table}')",
    ]
    return stmts


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--tables", default="all")
    p.add_argument("--snapshot-days", type=int, default=7)
    args = p.parse_args(argv)
    spark = session("iceberg-maintenance")
    targets = tables(spark) if args.tables == "all" else args.tables.split(",")
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    for t in targets:
        for sql in maintenance_sql(t, now, args.snapshot_days):
            print(f"[{t}] {sql[:120]}")
            for row in spark.sql(sql).collect():
                print(f"    {row.asDict()}")


if __name__ == "__main__":
    main()
