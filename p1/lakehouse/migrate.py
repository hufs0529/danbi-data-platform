"""레이크하우스 스키마 마이그레이션 (Athena).

    python -m p1.lakehouse.migrate plan
    python -m p1.lakehouse.migrate apply

두 종류를 다룬다.
1. 손으로 쓴 DDL(ddl/NNN_*.sql): ops·bronze. 버전·체크섬 추적(ops.schema_migrations)
2. 계약에서 생성되는 Silver 테이블: 없으면 CREATE, 있으면 계약과 Glue 스키마를 비교해
   - 새 컬럼 → ALTER TABLE ADD COLUMNS (호환 변경, S-C1)
   - 타입 변경·컬럼 삭제 → 거부. 계약의 change_policy(v2 데이터셋 병행)를 따른다
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from dataclasses import dataclass, field
from pathlib import Path

from contracts.registry import Contract, athena_ddl, load_all, silver_columns
from p1.sources.migrate import split_sql

DDL_DIR = Path(__file__).with_name("ddl")


@dataclass
class TablePlan:
    dataset: str
    create: str | None = None
    add_columns: list[tuple[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)  # 분류 대기(계약 PR 필요)


def _norm_type(t: str) -> str:
    return t.replace(" ", "").lower()


def plan_contract(
    c: Contract,
    existing: list[tuple[str, str]] | None,
    bucket: str,
    unclassified: frozenset[str] = frozenset(),
) -> TablePlan:
    """unclassified: Silver 잡이 자동 추가한 계약 밖 컬럼(ops.schema_drift). 오류가 아니라 '분류 대기'다."""
    tp = TablePlan(c.dataset)
    if existing is None:
        tp.create = athena_ddl(c, bucket)
        return tp
    have = {n: _norm_type(t) for n, t in existing}
    want = silver_columns(c)
    for name, typ in want:
        if name not in have:
            tp.add_columns.append((name, typ))
        elif have[name] != _norm_type(typ):
            tp.errors.append(
                f"{c.dataset}.{name}: 타입 변경 {have[name]} → {typ} 거부(비호환). v2 데이터셋 병행 절차를 따른다"
            )
    wanted = {n for n, _ in want}
    for name in have:
        if name in unclassified:
            tp.pending.append(name)
        elif name not in wanted:
            tp.errors.append(
                f"{c.dataset}.{name}: 계약에서 빠졌다. 컬럼 삭제는 비호환이다(소비자 확인 후 deprecated 표시 → 다음 메이저)"
            )
    return tp


def alter_sql(tp: TablePlan) -> str | None:
    if not tp.add_columns:
        return None
    cols = ", ".join(f"{n} {t}" for n, t in tp.add_columns)
    return f"ALTER TABLE {tp.dataset} ADD COLUMNS ({cols})"


def render_sql_files(buckets: dict[str, str], ddl_dir: Path = DDL_DIR) -> list[tuple[str, str, list[str]]]:
    out = []
    for p in sorted(ddl_dir.glob("*.sql")):
        text = p.read_text(encoding="utf-8")
        checksum = hashlib.sha256(text.encode()).hexdigest()
        stmts = [s.format(**buckets) for s in split_sql(text)]
        out.append((p.stem, checksum, stmts))
    return out


# ---------------------------------------------------------------------------
def _glue_columns(glue, dataset: str) -> list[tuple[str, str]] | None:
    db, table = dataset.split(".")
    try:
        t = glue.get_table(DatabaseName=db, Name=table)["Table"]
    except glue.exceptions.EntityNotFoundException:
        return None
    sd = t["StorageDescriptor"]
    return [(c["Name"], c["Type"]) for c in sd["Columns"]]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["plan", "apply"])
    p.add_argument("--project", default="danbi")
    p.add_argument("--env", default="dev")
    args = p.parse_args(argv)

    import boto3

    from danbi_common import athena
    from danbi_common.aws import get_parameter

    prefix = f"/{args.project}/{args.env}/p1/lakehouse"
    buckets = {k: get_parameter(f"{prefix}/{k}") for k in ("bronze_bucket", "silver_bucket", "gold_bucket")}
    wg = get_parameter(f"{prefix}/workgroup_pipeline")
    glue = boto3.client("glue")

    def q(sql: str):
        return athena.run(sql, wg)

    # 1) 손으로 쓴 DDL
    applied: dict[str, str] = {}
    if _glue_columns(glue, "ops.schema_migrations") is not None:
        applied = {r[0]: r[1] for r in q("SELECT version, checksum FROM ops.schema_migrations").rows}
    rc = 0
    for version, checksum, stmts in render_sql_files(buckets):
        if version in applied:
            if applied[version] != checksum:
                print(f"! {version}: 적용 후 파일이 바뀌었다. 새 번호로 추가한다")
                rc = 1
            continue
        print(f"+ ddl {version} ({len(stmts)} statements)")
        if args.action == "apply":
            for s in stmts:
                q(s)
            q(f"INSERT INTO ops.schema_migrations VALUES ('{version}', '{checksum}', current_timestamp)")

    # 2) 계약 기반 Silver
    drift: dict[str, set[str]] = {}
    if _glue_columns(glue, "ops.schema_drift") is not None:
        for ds, col in q("SELECT dataset, column_name FROM ops.schema_drift WHERE status = 'PENDING'").rows:
            drift.setdefault(ds, set()).add(col)
    for c in load_all():
        if c.layer != "silver":
            continue
        unclassified = frozenset(drift.get(c.dataset, set()))
        tp = plan_contract(c, _glue_columns(glue, c.dataset), buckets["silver_bucket"], unclassified)
        for col in tp.pending:
            print(f"? {c.dataset}.{col}: 분류 대기(계약에 추가하고 classification 을 정한다)")
        for e in tp.errors:
            print(f"! {e}")
            rc = 1
        if tp.create:
            print(f"+ create {c.dataset}")
            if args.action == "apply":
                q(tp.create)
        if (sql := alter_sql(tp)) is not None:
            print(f"~ {sql}")
            if args.action == "apply":
                q(sql)
    return rc


if __name__ == "__main__":
    sys.exit(main())
