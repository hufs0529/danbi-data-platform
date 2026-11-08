"""Debezium 신호 보내기: 증분 스냅샷(S-C3, F-03 갭 메우기).

    # 2천만 행 order_items 를 운영 중에 다시 읽기(락 없음, 스트리밍과 병행)
    python -m p1.cdc.signal --engine mysql snapshot commerce.order_items
    # F-03: binlog 유실 구간만 다시 읽기
    python -m p1.cdc.signal --engine mysql snapshot commerce.orders --where "updated_at >= '2026-10-29 00:00:00'"
    python -m p1.cdc.signal --engine mysql stop commerce.order_items

신호는 소스 DB 의 debezium_signal 테이블에 INSERT 한다. 커넥터가 binlog/WAL 에서 이 INSERT 를 읽고,
청크 단위로 SELECT 하면서 같은 시간대의 스트리밍 변경과 워터마크로 겹침을 정리한다(DBLog 알고리즘).
"""

from __future__ import annotations

import argparse
import json
import uuid


def snapshot_signal(tables: list[str], where: str | None = None, kind: str = "execute-snapshot") -> dict:
    data: dict = {"data-collections": tables, "type": "incremental"}
    if where:
        if ";" in where:
            raise ValueError("조건식에 ; 를 쓸 수 없다")
        data["additional-conditions"] = [{"data-collection": t, "filter": where} for t in tables]
    return {"id": f"sig-{uuid.uuid4()}", "type": kind, "data": json.dumps(data, ensure_ascii=False)}


def insert_sql(engine: str) -> str:
    table = "commerce.debezium_signal" if engine == "mysql" else "public.debezium_signal"
    return f"INSERT INTO {table} (id, type, data) VALUES (%s, %s, %s)"


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--engine", choices=["mysql", "postgres"], required=True)
    p.add_argument("--secret", help="기본: danbi-dev/p1/<engine>/debezium")
    p.add_argument("action", choices=["snapshot", "stop"])
    p.add_argument("tables", nargs="+")
    p.add_argument("--where")
    args = p.parse_args(argv)

    sig = snapshot_signal(
        args.tables, args.where, "execute-snapshot" if args.action == "snapshot" else "stop-snapshot"
    )
    from p1.generator.cli import _connect

    conn = _connect(args.secret or f"danbi-dev/p1/{args.engine}/debezium")
    with conn.cursor() as cur:
        cur.execute(insert_sql(args.engine), (sig["id"], sig["type"], sig["data"]))
    conn.commit()
    print(f"signal {sig['id']} {sig['type']} {sig['data']}")


if __name__ == "__main__":
    main()
