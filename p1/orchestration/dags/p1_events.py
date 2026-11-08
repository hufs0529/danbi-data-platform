"""트리거 이벤트 해석(순수 함수, Airflow import 없음 → 저장소 테스트에서 바로 검증)."""

from __future__ import annotations

import contextlib
import json
import re

KEY_RE = re.compile(r"pg_settlement/dt=(\d{4}-\d{2}-\d{2})/(danbipay_settle_\d{8}_\d{2}\.csv)$")


def _walk(node, out: list[dict]) -> None:
    if isinstance(node, str):
        s = node.strip()
        if s.startswith("{") and '"detail"' in s:
            with contextlib.suppress(ValueError):
                _walk(json.loads(s), out)
        return
    if isinstance(node, dict):
        if isinstance(node.get("detail"), dict) and "object" in node["detail"]:
            out.append(node)
        for v in node.values():
            _walk(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk(v, out)


def extract_objects(extras: list) -> list[dict]:
    """SQS 메시지 묶음(EventBridge 'Object Created' 이벤트가 Body 문자열로 들어 있음)에서 정산 파일만 뽑는다.

    같은 파일이 두 번 오면(재전송) 버전 ID 가 달라 둘 다 남는다 → 적재기가 내용 해시로 판정한다.
    """
    events: list[dict] = []
    _walk(extras, events)
    out, seen = [], set()
    for e in events:
        b = e["detail"].get("bucket", {}).get("name")
        o = e["detail"].get("object", {})
        k = o.get("key")
        ident = (b, k, o.get("version-id"))
        if b and k and KEY_RE.search(k) and ident not in seen:
            seen.add(ident)
            out.append({"bucket": b, "key": k, "version_id": o.get("version-id")})
    return out


def settle_dates(objs: list[dict]) -> list[str]:
    return sorted({KEY_RE.search(o["key"]).group(1) for o in objs})
