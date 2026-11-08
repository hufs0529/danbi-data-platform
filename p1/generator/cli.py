"""데이터 생성기 CLI.

    python -m p1.generator.cli dry-run --seconds 300 --faults late=0.05,ooo=0.02
    python -m p1.generator.cli run --seed-catalog --duration 3600 --orders-per-minute 30
    python -m p1.generator.cli settle --date 2026-10-29 --faults missing=0.01,skew=0.01,extra=2 --upload
    python -m p1.generator.cli schema-change --kind add      # S-C1

환경변수(ECS 태스크 정의에 들어 있다)
    GEN_MYSQL_SECRET, GEN_PG_SECRET   Secrets Manager ID (generator 계정)
    MSK_BOOTSTRAP 또는 MSK_BOOTSTRAP_PARAM(SSM 이름), SCHEMA_REGISTRY_URL
    LANDING_BUCKET                    PG 정산 파일 드롭 위치(SFTP 수신 서버 대신)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import time
from decimal import Decimal
from pathlib import Path

from danbi_common.logs import get_logger

from .faults import Faults
from .settlement import SETTLEMENT_SQL, SettlementFaults, build_files, txns_from_rows, window_utc
from .simulate import MemberState, Simulator
from .sinks import KafkaClickSink, MemorySink, SqlSink

log = get_logger("generator")
AVRO_DIR = Path(__file__).resolve().parents[2] / "contracts" / "avro"


def _connect(secret_id: str):
    from danbi_common.aws import get_secret_json

    s = get_secret_json(secret_id)
    if s["engine"] == "mysql":
        import pymysql

        return pymysql.connect(host=s["host"], port=int(s["port"]), user=s["username"], password=s["password"],
                               database=s["dbname"], ssl={"ssl": {}}, autocommit=False, charset="utf8mb4",
                               init_command="SET time_zone = '+00:00'")  # fmt: skip
    import psycopg

    return psycopg.connect(host=s["host"], port=int(s["port"]), user=s["username"], password=s["password"],
                           dbname=s["dbname"], sslmode="require", autocommit=False)  # fmt: skip


def hydrate(sim: Simulator, mysql_conn) -> None:
    """재시작 시 기존 회원·상품을 불러와 이어서 생성한다(같은 회원이 계속 주문하도록)."""
    with mysql_conn.cursor() as cur:
        cur.execute(
            "SELECT member_id, grade FROM members WHERE status = 'ACTIVE' ORDER BY member_id DESC LIMIT 20000"
        )
        for mid, grade in cur.fetchall():
            sim.world.members[mid] = MemberState(mid, grade)
        cur.execute("SELECT member_id, address_id FROM member_addresses ORDER BY address_id DESC LIMIT 40000")
        for mid, aid in cur.fetchall():
            if mid in sim.world.members:
                sim.world.members[mid].address_ids.append(aid)
        cur.execute("SELECT product_id, sale_price FROM product_prices")
        sim.world.products = {pid: Decimal(p) for pid, p in cur.fetchall()}
        cur.execute("SELECT coupon_id, discount_type, discount_value FROM coupons WHERE valid_to > NOW()")
        sim.world.coupons = {cid: (k, Decimal(v)) for cid, k, v in cur.fetchall()}


def _bootstrap() -> str | None:
    """MSK 는 이 태스크 정의보다 나중 스택(p1-20-streaming)에서 만들어진다. 그래서 실행 시점에 SSM 에서 찾는다."""
    if os.environ.get("MSK_BOOTSTRAP"):
        return os.environ["MSK_BOOTSTRAP"]
    param = os.environ.get("MSK_BOOTSTRAP_PARAM")
    if not param:
        return None
    from danbi_common.aws import get_parameter

    try:
        value = get_parameter(param)
    except Exception as e:  # ParameterNotFound: 아직 MSK 가 없다
        log.info(f"MSK 부트스트랩 파라미터 없음({type(e).__name__})")
        return None
    return None if value == "disabled" else value  # make pause 로 MSK 를 지운 상태


def _click_sink() -> KafkaClickSink | None:
    bootstrap = _bootstrap()
    if not bootstrap:
        log.info("MSK 없음 → 클릭 전송 생략, DB 쓰기만 한다")
        return None
    from confluent_kafka import Producer
    from confluent_kafka.schema_registry import SchemaRegistryClient
    from confluent_kafka.schema_registry.avro import AvroSerializer

    from danbi_common.kafka import producer_config

    sr = SchemaRegistryClient({"url": os.environ["SCHEMA_REGISTRY_URL"]})
    schema = (AVRO_DIR / "click_event.avsc").read_text(encoding="utf-8")
    # auto.register.schemas=False: 스키마 등록은 CI(계약 검사)만 한다. 생성기가 몰래 새 버전을 올리지 못한다
    ser = AvroSerializer(sr, schema, conf={"auto.register.schemas": False, "use.latest.version": True})
    return KafkaClickSink(Producer(producer_config(bootstrap)), ser)


def cmd_dry_run(args) -> None:
    sim = Simulator(seed=args.seed, faults=Faults.parse(args.faults), orders_per_minute=args.orders_per_minute,
                    clicks_per_second=args.clicks_per_second)  # fmt: skip
    sink = MemorySink()
    sink.apply(sim.seed_catalog())
    sink.apply(sim.run(args.seconds))
    print(json.dumps(dict(sorted(sink.counts.items())), ensure_ascii=False, indent=2))


def cmd_run(args) -> None:
    faults = Faults.parse(args.faults)
    sim = Simulator(seed=args.seed or int(time.time()), faults=faults, orders_per_minute=args.orders_per_minute,
                    clicks_per_second=args.clicks_per_second, worker=args.worker)  # fmt: skip
    conns = {
        "commerce": _connect(os.environ["GEN_MYSQL_SECRET"]),
        "payment": _connect(os.environ["GEN_PG_SECRET"]),
    }
    sql = SqlSink(conns, {"commerce": "mysql", "payment": "postgres"})
    clicks = _click_sink()
    if args.seed_catalog:
        sql.apply(sim.seed_catalog())
    else:
        hydrate(sim, conns["commerce"])
    log.info(f"start: members={len(sim.world.members)} products={len(sim.world.products)} faults={faults}")

    deadline = time.monotonic() + args.duration if args.duration else None
    while deadline is None or time.monotonic() < deadline:
        sim.now = dt.datetime.now(dt.UTC).replace(microsecond=0)  # 가상 시계를 벽시계에 맞춘다
        actions = sim.tick()
        sql.apply(actions)
        if clicks:
            clicks.apply(actions, sim.now)
        if sim.ticks % 60 == 0:
            log.info(f"tick={sim.ticks} open_payments={len(sim.world.payments)}")
        time.sleep(max(0.0, 1 - (time.time() % 1)))
    if clicks:
        clicks.producer.flush(30)


def cmd_settle(args) -> None:
    d = dt.date.fromisoformat(args.date)
    f = SettlementFaults()
    for part in filter(None, (args.faults or "").split(",")):
        k, _, v = part.partition("=")
        if k == "missing":
            f.missing_ratio = float(v)
        elif k == "skew":
            f.amount_skew_ratio = float(v)
        elif k == "extra":
            f.extra_count = int(v)
        elif k == "header_v2":
            f.header_version = 2
        elif k in ("broken_encoding", "duplicate", "correction"):
            setattr(f, k, True)
        else:
            raise ValueError(k)
    conn = _connect(os.environ["GEN_PG_SECRET"])
    lo, hi = window_utc(d)
    with conn.cursor() as cur:
        cur.execute(SETTLEMENT_SQL, (lo, hi))
        txns = txns_from_rows(cur.fetchall())
    files = build_files(d, txns, f, seed=int(d.strftime("%Y%m%d")))
    if not args.upload:
        out = Path(args.out_dir)
        out.mkdir(parents=True, exist_ok=True)
        for i, (name, body) in enumerate(files):
            (out / f"{i}_{name}").write_bytes(body)
        log.info(f"{len(files)} files → {out}")
        return
    import boto3

    s3 = boto3.client("s3")
    for name, body in files:
        key = f"pg_settlement/dt={d}/{name}"
        s3.put_object(Bucket=os.environ["LANDING_BUCKET"], Key=key, Body=body, ContentType="text/csv")
        log.info(f"uploaded s3://{os.environ['LANDING_BUCKET']}/{key} ({len(body)} bytes)")
        time.sleep(2)  # 중복 전송은 실제로도 몇 초 간격으로 온다


def cmd_schema_change(args) -> None:
    sql = {
        "add": "ALTER TABLE orders ADD COLUMN gift_message VARCHAR(200) NULL",
        "breaking": "ALTER TABLE orders MODIFY COLUMN discount_amount VARCHAR(20)",
    }[args.kind]
    conn = _connect(os.environ["GEN_MYSQL_SECRET"])
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
    log.info(f"applied: {sql}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="generator")
    sub = p.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--seed", type=int, default=0)
    common.add_argument("--faults", default="")
    common.add_argument("--orders-per-minute", type=float, default=30)
    common.add_argument("--clicks-per-second", type=float, default=20)

    d = sub.add_parser("dry-run", parents=[common])
    d.add_argument("--seconds", type=int, default=300)
    d.set_defaults(func=cmd_dry_run)

    r = sub.add_parser("run", parents=[common])
    r.add_argument("--duration", type=int, default=0, help="초. 0 이면 계속")
    r.add_argument("--seed-catalog", action="store_true")
    r.add_argument("--worker", type=int, default=0)
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("settle")
    s.add_argument("--date", required=True)
    s.add_argument("--faults", default="")
    s.add_argument("--upload", action="store_true")
    s.add_argument("--out-dir", default="out/settlement")
    s.set_defaults(func=cmd_settle)

    c = sub.add_parser("schema-change")
    c.add_argument("--kind", choices=["add", "breaking"], required=True)
    c.set_defaults(func=cmd_schema_change)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
