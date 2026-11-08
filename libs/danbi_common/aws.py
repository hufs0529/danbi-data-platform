"""비밀·파라미터 조회. 호출 빈도가 높은 곳(Flink UDF, API)에서 쓰도록 TTL 캐시를 둔다."""

from __future__ import annotations

import json
import time
from typing import Any

_cache: dict[str, tuple[float, Any]] = {}


def _cached(key: str, ttl: float, loader):
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    value = loader()
    _cache[key] = (now, value)
    return value


def get_secret_string(secret_id: str, client=None, ttl: float = 300) -> str:
    def load() -> str:
        sm = client
        if sm is None:
            import boto3

            sm = boto3.client("secretsmanager")
        return sm.get_secret_value(SecretId=secret_id)["SecretString"]

    return _cached(f"secret:{secret_id}", ttl, load)


def get_secret_json(secret_id: str, client=None, ttl: float = 300) -> dict[str, Any]:
    return json.loads(get_secret_string(secret_id, client=client, ttl=ttl))


def get_parameter(name: str, client=None, ttl: float = 300) -> str:
    def load() -> str:
        ssm = client
        if ssm is None:
            import boto3

            ssm = boto3.client("ssm")
        return ssm.get_parameter(Name=name)["Parameter"]["Value"]

    return _cached(f"param:{name}", ttl, load)


def clear_cache() -> None:
    _cache.clear()
