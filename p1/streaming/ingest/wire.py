"""Confluent 와이어 포맷: [0x00][스키마 ID 4바이트 big-endian][Avro 바이너리].

Flink 의 Kafka 소스는 값을 raw 바이트로 받고, 여기서 직접 해석한다. 이렇게 하면
- 해석 실패(독약 메시지)를 잡을 수 있어 DLQ 로 보낼 수 있다(F-04). 포맷 역직렬화기에 맡기면 잡 전체가 실패한다.
- 토픽마다 다른 스키마(CDC 테이블 수십 개)를 하나의 잡에서 처리한다.
"""

from __future__ import annotations

import io
import json
import struct
import urllib.request
from collections.abc import Callable

import fastavro

MAGIC = 0


class WireError(ValueError):
    pass


class SchemaCache:
    def __init__(self, fetch: Callable[[int], str]) -> None:
        self._fetch = fetch
        self._parsed: dict[int, object] = {}

    def get(self, schema_id: int):
        if schema_id not in self._parsed:
            self._parsed[schema_id] = fastavro.parse_schema(json.loads(self._fetch(schema_id)))
        return self._parsed[schema_id]


def http_fetcher(base_url: str, timeout: float = 5.0) -> Callable[[int], str]:
    def fetch(schema_id: int) -> str:
        url = f"{base_url.rstrip('/')}/schemas/ids/{schema_id}"
        with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310 (내부 http, 고정 호스트)
            return json.loads(r.read())["schema"]

    return fetch


def decode(payload: bytes | None, cache: SchemaCache) -> tuple[int, object]:
    if payload is None:
        raise WireError("빈 메시지(툼스톤)")
    if len(payload) < 5:
        raise WireError(f"너무 짧다({len(payload)}B)")
    if payload[0] != MAGIC:
        raise WireError(f"매직 바이트 {payload[0]} != 0 (Confluent 와이어 포맷 아님)")
    schema_id = struct.unpack(">I", payload[1:5])[0]
    try:
        record = fastavro.schemaless_reader(io.BytesIO(payload[5:]), cache.get(schema_id))
    except Exception as e:
        raise WireError(f"Avro 해석 실패(schema_id={schema_id}): {e}") from e
    return schema_id, record


def encode(record: object, schema: dict, schema_id: int) -> bytes:
    """테스트·DLQ 재처리 도구용."""
    buf = io.BytesIO()
    buf.write(bytes([MAGIC]) + struct.pack(">I", schema_id))
    fastavro.schemaless_writer(buf, fastavro.parse_schema(schema), record)
    return buf.getvalue()
