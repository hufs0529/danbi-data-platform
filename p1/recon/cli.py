"""대사 잡 진입점 (ECS RunTask, MWAA 가 호출).

python -m p1.recon.cli load-settlement --bucket B --key pg_settlement/dt=2026-10-29/danbipay_settle_20261029_01.csv
python -m p1.recon.cli recon-pg --date 2026-10-29        # D 와 D-1(보류 건 재판정)을 함께 돌린다
python -m p1.recon.cli recon-source                       # 1시간 창, 30분 지연 여유
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from dataclasses import asdict
from decimal import Decimal

from danbi_common.logs import get_logger

from . import classify as C
from . import settlement as S
from . import source_check as SC

log = get_logger("recon")
REGION = os.environ.get("AWS_REGION", "ap-northeast-2")
WORKGROUP = os.environ.get("ATHENA_WORKGROUP", "danbi-dev-pipeline")
KST = dt.timezone(dt.timedelta(hours=9))


def _emit(event: dict) -> None:
    print(json.dumps(event, ensure_ascii=False, default=str))  # 마지막 줄 = MWAA XCom 로 읽는 결과


def _metric(name: str, value: float, dims: dict[str, str]) -> None:
    import boto3

    boto3.client("cloudwatch", region_name=REGION).put_metric_data(
        Namespace="Danbi/Recon",
        MetricData=[
            {
                "MetricName": name,
                "Value": value,
                "Dimensions": [{"Name": k, "Value": v} for k, v in dims.items()],
            }
        ],
    )


# ---------------------------------------------------------------------------
def load_settlement(bucket: str, key: str, version_id: str | None) -> dict:
    import boto3

    from . import store

    s3 = boto3.client("s3", region_name=REGION)
    obj = s3.get_object(Bucket=bucket, Key=key, **({"VersionId": version_id} if version_id else {}))
    body = obj["Body"].read()
    cat = store.catalog(REGION)
    files = store.read(cat, "bronze.pg_settlement_files")
    known = {f["sha256"] for f in files if f["status"] in ("ACCEPTED", "SUPERSEDED", "QUARANTINED")}
    v = S.inspect(key.rsplit("/", 1)[-1], body, known)
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    file_row = {"s3_key": key, "s3_version_id": obj.get("VersionId"), "file_name": v.file_name,
                "settle_date": v.settle_date, "seq": v.seq, "sha256": v.sha256, "size_bytes": v.size_bytes,
                "header_version": v.header_version, "row_count": len(v.lines), "trailer_count": v.trailer_count,
                "trailer_amount": v.trailer_amount, "status": v.status, "reason": v.reason, "received_at": now}  # fmt: skip

    if v.status == "QUARANTINED":
        q = f"quarantine/{key}"
        s3.copy_object(Bucket=bucket, Key=q, CopySource={"Bucket": bucket, "Key": key,
                       **({"VersionId": obj["VersionId"]} if obj.get("VersionId") else {})})  # fmt: skip
        store.append(cat, "bronze.pg_settlement_files", [file_row])
        _metric("QuarantinedFiles", 1, {"Feed": "pg_settlement"})
        return {"status": v.status, "reason": v.reason, "quarantine_key": q}
    if v.status == "DUPLICATE":
        store.append(cat, "bronze.pg_settlement_files", [file_row])
        return {"status": v.status, "sha256": v.sha256}

    # ACCEPTED: 라인 → 파일 대장 → 유효본 재계산(Silver). 각 단계가 멱등이라 중간에 실패해도 다시 돌리면 된다
    lines = [{**asdict(ln), "file_sha256": v.sha256, "loaded_at": now} for ln in v.lines]
    store.overwrite(cat, "bronze.pg_settlement_lines", lines, f"file_sha256 == '{v.sha256}'")
    store.overwrite(
        cat, "bronze.pg_settlement_files", [file_row], f"sha256 == '{v.sha256}' and status == 'ACCEPTED'"
    )
    d = v.settle_date.isoformat()
    accepted = {f["seq"]: f["sha256"] for f in store.read(cat, "bronze.pg_settlement_files", f"settle_date == '{d}'")
                if f["status"] in ("ACCEPTED", "SUPERSEDED")}  # fmt: skip
    eff = S.effective_seq(accepted)
    eff_sha = accepted[eff]
    effective = [ln for ln in store.read(cat, "bronze.pg_settlement_lines", f"file_sha256 == '{eff_sha}'")]
    silver_rows = [{"settle_date": ln["settle_date"], "txn_at": ln["txn_at"], "payment_key": ln["payment_key"],
                    "order_no": ln["order_no"], "kind": ln["kind"], "method": ln["method"], "issuer": ln["issuer"],
                    "amount": ln["amount"], "fee": ln["fee"], "vat": ln["vat"], "net": ln["net"],
                    "file_seq": eff, "file_sha256": eff_sha, "_run_id": f"load-{v.sha256[:12]}"}
                   for ln in effective]  # fmt: skip
    store.overwrite(cat, "silver.pg_settlement", silver_rows, f"settle_date == '{d}'")
    if eff != v.seq:
        log.info(f"{v.file_name}: 이미 더 높은 차수({eff})가 유효본이다")
    return {"status": "ACCEPTED", "settle_date": d, "seq": v.seq, "effective_seq": eff, "rows": len(lines)}


# ---------------------------------------------------------------------------
INTERNAL_SQL = """
SELECT p.payment_key,
       CASE WHEN h.to_status = 'CAPTURED' THEN 'CAPTURE' ELSE 'CANCEL' END AS kind,
       CAST(h.amount AS varchar) AS amount,
       p.method,
       CAST(date((with_timezone(h.occurred_at, 'UTC') AT TIME ZONE 'Asia/Seoul') + INTERVAL '30' MINUTE) AS varchar) AS settle_date
FROM silver.payment_status_history h
JOIN silver.payments p ON p.payment_id = h.payment_id
WHERE h.to_status IN ('CAPTURED', 'PARTIAL_CANCELED', 'CANCELED')
  AND NOT coalesce(h.is_deleted, false)
  AND h.occurred_at >= ? AND h.occurred_at < ?
"""


def _ts_param(t: dt.datetime) -> str:
    return f"TIMESTAMP '{t:%Y-%m-%d %H:%M:%S}'"


def recon_pg(d: dt.date) -> dict:
    from danbi_common import athena

    from . import store

    start = (
        dt.datetime.combine(d - dt.timedelta(days=2), dt.time(23, 30), KST)
        .astimezone(dt.UTC)
        .replace(tzinfo=None)
    )
    end = dt.datetime.combine(d, dt.time(23, 30), KST).astimezone(dt.UTC).replace(tzinfo=None)
    rows = athena.run(INTERNAL_SQL, WORKGROUP, params=[_ts_param(start), _ts_param(end)]).dicts()
    internal = [r for r in rows if r["settle_date"] == d.isoformat()]
    prev_keys = {
        (r["payment_key"], r["kind"])
        for r in rows
        if r["settle_date"] == (d - dt.timedelta(days=1)).isoformat()
    }

    cat = store.catalog(REGION)
    pg = store.read(cat, "silver.pg_settlement", f"settle_date == '{d.isoformat()}'")
    nxt = store.read(
        cat, "silver.pg_settlement", f"settle_date == '{(d + dt.timedelta(days=1)).isoformat()}'"
    )
    next_keys = {(r["payment_key"], r["kind"]) for r in nxt} if nxt else None
    pg_only = {r["payment_key"] for r in pg} - {r["payment_key"] for r in internal}
    known: set[str] = set()
    if pg_only:
        in_list = ",".join(f"'{k}'" for k in sorted(pg_only) if k.replace("_", "").isalnum())
        known = {r["payment_key"] for r in athena.run(
            f"SELECT payment_key FROM silver.payments WHERE payment_key IN ({in_list})", WORKGROUP).dicts()}  # fmt: skip

    results = C.classify(d, internal, pg, next_keys, prev_keys, known)
    run_id = f"recon-{d}-{dt.datetime.now(dt.UTC):%H%M%S}"
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    store.overwrite(cat, "gold.recon_pg_settlement",
                    [{**asdict(r), "run_id": run_id, "created_at": now} for r in results],
                    f"settle_date == '{d.isoformat()}'")  # fmt: skip
    s = C.summary(results)
    for name in ("unexplained_count", "pending"):
        _metric("UnexplainedCount" if name == "unexplained_count" else "PendingCount", float(s[name]),
                {"Feed": "pg_settlement"})  # fmt: skip
    _metric("UnexplainedAmount", float(Decimal(s["unexplained_amount"])), {"Feed": "pg_settlement"})
    import boto3

    boto3.client("s3", region_name=REGION).put_object(
        Bucket=os.environ["GOLD_BUCKET"], Key=f"_reports/recon/dt={d}/report.md",
        Body=C.render_report(d, results).encode(), ContentType="text/markdown; charset=utf-8",
    )  # fmt: skip
    return {"settle_date": d.isoformat(), **s}


# ---------------------------------------------------------------------------
def recon_source(now: dt.datetime) -> dict:
    from danbi_common import athena

    from . import store

    w0, w1 = SC.window(now)
    outcomes = []
    conns = {}
    for c in SC.CHECKS:
        engine = "mysql" if c.source_db == "commerce" else "postgres"
        if engine not in conns:
            from p1.generator.cli import _connect

            conns[engine] = _connect(os.environ[f"RECON_{engine.upper()}_SECRET"])
        with conns[engine].cursor() as cur:
            cur.execute(SC.source_sql(c, engine), (w0, w1))
            sc, sa = cur.fetchone()
        amount = f"coalesce(sum({c.amount_col}), 0)" if c.amount_col else "0"
        lake = athena.run(
            f"SELECT count(*) AS n, CAST({amount} AS varchar) AS a FROM {c.silver} "
            f"WHERE {c.ts_col} >= ? AND {c.ts_col} < ? AND NOT coalesce(is_deleted, false)",
            WORKGROUP, params=[_ts_param(w0), _ts_param(w1)],
        ).dicts()[0]  # fmt: skip
        outcomes.append(SC.Outcome(c, w0, w1, int(sc), int(lake["n"]), Decimal(sa), Decimal(lake["a"])))
    rows = [{"check_table": o.check.silver, "window_start": o.window_start, "window_end": o.window_end,
             "source_count": o.source_count, "lake_count": o.lake_count, "source_amount": o.source_amount,
             "lake_amount": o.lake_amount, "ok": o.ok, "checked_at": dt.datetime.now(dt.UTC).replace(tzinfo=None)}
            for o in outcomes]  # fmt: skip
    store.append(store.catalog(REGION), "ops.recon_source_results", rows)
    bad = [o for o in outcomes if not o.ok]
    _metric("SourceLakeMismatch", float(len(bad)), {"Feed": "cdc"})
    return {"window": [w0, w1], "mismatches": [o.check.silver for o in bad]}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("load-settlement")
    a.add_argument("--bucket", required=True)
    a.add_argument("--key", required=True)
    a.add_argument("--version-id")
    b = sub.add_parser("recon-pg")
    b.add_argument("--date", help="정산일(KST). 기본: 어제")
    b.add_argument("--with-previous", action="store_true", default=True)
    sub.add_parser("recon-source")
    args = p.parse_args(argv)

    if args.cmd == "load-settlement":
        _emit(load_settlement(args.bucket, args.key, args.version_id))
    elif args.cmd == "recon-pg":
        d = (
            dt.date.fromisoformat(args.date)
            if args.date
            else dt.datetime.now(KST).date() - dt.timedelta(days=1)
        )
        out = (
            [recon_pg(d - dt.timedelta(days=1))] if args.with_previous else []
        )  # 어제 보류(PG_PENDING) 재판정
        out.append(recon_pg(d))
        _emit({"results": out})
        return 1 if out[-1]["unexplained_count"] else 0
    else:
        res = recon_source(dt.datetime.now(dt.UTC).replace(tzinfo=None))
        _emit(res)
        return 1 if res["mismatches"] else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
