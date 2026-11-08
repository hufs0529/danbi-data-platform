"""토픽별 메시지 → Bronze 행. 순수 파이썬이라 Flink 없이 테스트한다."""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any

from .pii import PiiTransformer
from .wire import SchemaCache, WireError, decode

SKIP_TABLES = {"debezium_signal", "debezium_heartbeat"}
KNOWN_DBS = {"commerce", "payment"}
_FILE_NO = re.compile(r"(\d+)$")


def _json(d: dict | None) -> str | None:
    if d is None:
        return None
    return json.dumps(d, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _utc(ms: int | None) -> dt.datetime | None:
    if ms is None:
        return None
    return dt.datetime.fromtimestamp(ms / 1000, dt.UTC).replace(
        tzinfo=None
    )  # Iceberg timestamp(UTC, tz 없음)


def _parse_ts(v: Any) -> dt.datetime | None:
    """Debezium 시간 표현: ZonedTimestamp(ISO 문자열) | 에포크 ms | 에포크 µs."""
    if v is None:
        return None
    if isinstance(v, str):
        return dt.datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone(dt.UTC).replace(tzinfo=None)
    return _utc(v // 1000 if v > 10**14 else v)


def change_seq(source: dict[str, Any], op: str) -> str:
    """같은 소스 안에서 사전순 정렬 = 커밋 순서. 스냅샷 읽기(r)는 같은 위치의 스트리밍 변경보다 앞선다."""
    rank = 0 if op == "r" else 1
    if source.get("connector") == "postgresql" or "lsn" in source:
        return f"p{int(source.get('lsn') or 0):020d}{rank}"
    m = _FILE_NO.search(str(source.get("file") or ""))
    file_no = int(m.group(1)) if m else 0
    return f"m{file_no:08d}{int(source.get('pos') or 0):012d}{rank}{int(source.get('row') or 0):06d}"


class Decoder:
    def __init__(self, cache: SchemaCache, pii: PiiTransformer) -> None:
        self.cache = cache
        self.pii = pii

    # ------------------------------------------------------------------ CDC
    def cdc(self, topic: str, key: bytes | None, value: bytes | None) -> dict[str, Any] | None:
        schema_id, env = decode(value, self.cache)
        if not isinstance(env, dict) or "op" not in env or "source" not in env:
            raise WireError("Debezium envelope 가 아니다")
        src = env["source"]
        table = src.get("table")
        if table in SKIP_TABLES:
            return None
        op = env["op"]
        if op not in ("c", "u", "d", "r"):
            return None  # truncate(t)·message(m)는 Bronze 대상이 아님(운영에서 truncate 는 알림 대상)
        if src.get("db") not in KNOWN_DBS:
            raise WireError(f"알 수 없는 소스 DB {src.get('db')}")
        source_key = f"{src.get('db')}.{table}"
        key_rec = decode(key, self.cache)[1] if key else None
        return {
            "source_db": src.get("db"),
            "table_name": table,
            "op": op,
            "key_json": _json(self.pii.apply(source_key, key_rec)),  # type: ignore[arg-type]
            "before_json": _json(self.pii.apply(source_key, env.get("before"))),
            "after_json": _json(self.pii.apply(source_key, env.get("after"))),
            "change_seq": change_seq(src, op),
            "txn_id": str(src.get("gtid") or src.get("txId") or ""),
            "source_ts": _utc(src.get("ts_ms")),
            "is_snapshot": op == "r",
            "schema_id": schema_id,
        }

    # ------------------------------------------------------------------ 클릭
    def click(self, value: bytes | None) -> dict[str, Any]:
        schema_id, rec = decode(value, self.cache)
        rec = self.pii.apply("topic:clicks.raw", rec)  # type: ignore[arg-type]
        assert rec is not None
        out = {k: rec.get(k) for k in ("event_id", "event_type", "session_id", "member_id", "device_id", "platform",
                                       "page", "product_id", "search_query", "order_no", "user_agent")}  # fmt: skip
        for k in ("event_time", "sent_time"):
            v = rec.get(k)
            out[k] = v.astimezone(dt.UTC).replace(tzinfo=None) if isinstance(v, dt.datetime) else _utc(v)
        out["schema_id"] = schema_id
        return out

    # ------------------------------------------------------------------ Outbox 이벤트
    def domain_event(self, topic: str, key: bytes | None, value: bytes | None,
                     headers: dict[str, bytes] | None = None) -> dict[str, Any]:  # fmt: skip
        schema_id, rec = decode(value, self.cache)
        if not isinstance(rec, dict) or "payload" not in rec:
            raise WireError("outbox envelope 가 아니다")
        payload = json.loads(rec["payload"]) if isinstance(rec["payload"], str) else rec["payload"]
        payload = self.pii.apply(f"topic:{topic}", payload)
        occurred_at = _parse_ts(rec.get("occurredAt"))
        key_val = decode(key, self.cache)[1] if key else None
        event_id = (headers or {}).get("eventId")
        return {
            "topic": topic,
            "event_key": str(key_val) if key_val is not None else None,
            "event_id": event_id.decode() if isinstance(event_id, bytes) else event_id,
            "event_type": rec.get("eventType"),
            "payload_json": _json(payload),
            "occurred_at": occurred_at,
            "schema_id": schema_id,
        }
