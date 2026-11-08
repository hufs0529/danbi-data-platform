"""Schema Registry 운영 도구.

    python -m p1.streaming.schema_registry.admin config     # 전역·주제별 호환성 규칙 적용
    python -m p1.streaming.schema_registry.admin check      # 로컬 스키마가 등록된 최신과 호환되는지
    python -m p1.streaming.schema_registry.admin register   # check 통과 시에만 등록
    python -m p1.streaming.schema_registry.admin list

등록 권한은 이 도구(=배포 파이프라인)만 쓴다. 애플리케이션 프로듀서는 auto.register.schemas=false 다.
CDC 주제(cdc.*)는 Debezium 이 DB DDL 에서 스키마를 만들어 자동 등록한다. 비호환 DDL 이면 등록이 거부되고
커넥터 태스크가 실패한다 → 알림(S-C1). 이것이 런타임의 마지막 방어선이고, 앞선 방어선은 CI 계약 검사다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx

AVRO_DIR = Path(__file__).resolve().parents[3] / "contracts" / "avro"
CT = "application/vnd.schemaregistry.v1+json"

# 로컬 스키마 파일 → 주제(TopicNameStrategy: <topic>-value)
MANIFEST = {"click_event.avsc": "clicks.raw-value"}

# 전역 기본은 BACKWARD(새 스키마로 옛 데이터를 읽을 수 있어야 함 = 소비자 먼저 배포 가능)
GLOBAL_COMPATIBILITY = "BACKWARD"
SUBJECT_COMPATIBILITY = {
    # 클릭은 앱이 여러 버전으로 동시에 보낸다(구버전 앱이 몇 달 남음) → 앞뒤 모두 호환
    "clicks.raw-value": "FULL_TRANSITIVE",
}


class Registry:
    def __init__(self, url: str, client: httpx.Client | None = None) -> None:
        self.c = client or httpx.Client(base_url=url, timeout=10, headers={"Content-Type": CT})

    def set_compat(self, level: str, subject: str | None = None) -> None:
        path = f"/config/{subject}" if subject else "/config"
        self.c.put(path, json={"compatibility": level}).raise_for_status()

    def subjects(self) -> list[str]:
        r = self.c.get("/subjects")
        r.raise_for_status()
        return r.json()

    def is_compatible(self, subject: str, schema: str) -> tuple[bool, list[str]]:
        r = self.c.post(
            f"/compatibility/subjects/{subject}/versions/latest?verbose=true", json={"schema": schema}
        )
        if r.status_code == 404:  # 첫 버전
            return True, []
        r.raise_for_status()
        body = r.json()
        return bool(body.get("is_compatible")), body.get("messages", [])

    def register(self, subject: str, schema: str) -> int:
        r = self.c.post(f"/subjects/{subject}/versions", json={"schema": schema})
        r.raise_for_status()
        return int(r.json()["id"])


def local_schemas(avro_dir: Path = AVRO_DIR) -> dict[str, str]:
    out = {}
    for fname, subject in MANIFEST.items():
        # 공백을 정규화해 같은 스키마가 다른 버전으로 등록되는 것을 막는다
        out[subject] = json.dumps(
            json.loads((avro_dir / fname).read_text(encoding="utf-8")), separators=(",", ":")
        )
    return out


def run(action: str, reg: Registry) -> int:
    if action == "config":
        reg.set_compat(GLOBAL_COMPATIBILITY)
        for s, level in SUBJECT_COMPATIBILITY.items():
            reg.set_compat(level, s)
        print("compatibility set")
        return 0
    if action == "list":
        print("\n".join(reg.subjects()))
        return 0
    failed = False
    for subject, schema in local_schemas().items():
        ok, msgs = reg.is_compatible(subject, schema)
        print(f"{'OK  ' if ok else 'FAIL'} {subject} {' | '.join(msgs)}")
        failed |= not ok
        if action == "register" and ok:
            print(f"     registered id={reg.register(subject, schema)}")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["config", "check", "register", "list"])
    p.add_argument(
        "--url", default=os.environ.get("SCHEMA_REGISTRY_URL", "http://schema-registry.danbi.internal:8081")
    )
    args = p.parse_args(argv)
    return run(args.action, Registry(args.url))


if __name__ == "__main__":
    sys.exit(main())
