"""액션을 실제 시스템에 적용한다. DB 드라이버·Kafka 는 해당 싱크를 쓸 때만 import 한다."""

from __future__ import annotations

import datetime as dt
import heapq
import itertools
import json
from collections import Counter
from typing import Any

from .actions import Action, Click, Ddl, Op, Write

ALLOWED_TABLES = {
    "commerce": {"members", "member_addresses", "products", "product_prices", "coupons", "orders", "order_items",
                 "coupon_usages", "cs_tickets"},
    "payment": {"payments", "payment_status_history", "refunds", "outbox_events"},
}  # fmt: skip


def _q(engine: str, ident: str) -> str:
    if not ident.replace("_", "").isalnum():
        raise ValueError(f"식별자 오류: {ident}")
    return f"`{ident}`" if engine == "mysql" else f'"{ident}"'


def _value(engine: str, v: Any) -> Any:
    if isinstance(v, dt.datetime) and v.tzinfo is not None and engine == "mysql":
        return v.astimezone(dt.UTC).replace(tzinfo=None)  # 세션 time_zone=UTC
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False, default=str)
    return v


def op_sql(engine: str, db: str, op: Op) -> tuple[str, list[Any]]:
    if op.table not in ALLOWED_TABLES[db]:
        raise ValueError(f"{db}.{op.table} 는 생성기 대상이 아니다")
    t = _q(engine, op.table)
    if op.kind == "insert":
        cols = list(op.values)
        sql = f"INSERT INTO {t} ({', '.join(_q(engine, c) for c in cols)}) VALUES ({', '.join(['%s'] * len(cols))})"
        return sql, [_value(engine, op.values[c]) for c in cols]
    where = " AND ".join(f"{_q(engine, k)} = %s" for k in op.key)
    params = [_value(engine, v) for v in op.key.values()]
    if op.kind == "update":
        sets = ", ".join(f"{_q(engine, c)} = %s" for c in op.values)
        return f"UPDATE {t} SET {sets} WHERE {where}", [
            _value(engine, v) for v in op.values.values()
        ] + params
    return f"DELETE FROM {t} WHERE {where}", params


class MemorySink:
    """테스트·dry-run 용. 무엇이 몇 번 일어났는지 센다."""

    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()
        self.writes: list[Write] = []
        self.clicks: list[Click] = []
        self.ddls: list[Ddl] = []

    def apply(self, actions: list[Action]) -> None:
        for a in actions:
            if isinstance(a, Write):
                self.writes.append(a)
                for op in a.ops:
                    self.counts[f"{a.db}.{op.table}.{op.kind}"] += 1
            elif isinstance(a, Click):
                self.clicks.append(a)
                self.counts["clicks"] += 1
            else:
                self.ddls.append(a)
                self.counts["ddl"] += 1


class SqlSink:
    """Write 하나 = 트랜잭션 하나. outbox INSERT 가 결제 UPDATE 와 함께 커밋된다."""

    def __init__(self, conns: dict[str, Any], engines: dict[str, str]) -> None:
        self.conns = conns  # db → connection (autocommit 끔)
        self.engines = engines  # db → mysql|postgres

    def apply(self, actions: list[Action]) -> None:
        for a in actions:
            if isinstance(a, Write):
                conn = self.conns[a.db]
                engine = self.engines[a.db]
                with conn.cursor() as cur:
                    for op in a.ops:
                        sql, params = op_sql(engine, a.db, op)
                        cur.execute(sql, params)
                conn.commit()
            elif isinstance(a, Ddl):
                with self.conns[a.db].cursor() as cur:
                    cur.execute(a.sql)
                self.conns[a.db].commit()


class KafkaClickSink:
    """clicks.raw 로 Avro(Confluent 와이어 포맷) 전송. 지연 전송 이벤트는 deliver_at 까지 붙잡아 둔다."""

    def __init__(self, producer, serializer, topic: str = "clicks.raw") -> None:
        self.producer = producer
        self.serializer = serializer
        self.topic = topic
        self._pending: list[tuple[dt.datetime, int, dict]] = []
        self._seq = itertools.count()

    def apply(self, actions: list[Action], now: dt.datetime) -> int:
        for a in actions:
            if isinstance(a, Click):
                heapq.heappush(self._pending, (a.deliver_at, next(self._seq), a.event))
        return self.flush(now)

    def flush(self, now: dt.datetime) -> int:
        from confluent_kafka.serialization import MessageField, SerializationContext

        sent = 0
        while self._pending and self._pending[0][0] <= now:
            _, _, event = heapq.heappop(self._pending)
            value = self.serializer(event, SerializationContext(self.topic, MessageField.VALUE))
            self.producer.produce(self.topic, key=event["session_id"].encode(), value=value)
            sent += 1
        self.producer.poll(0)
        return sent
