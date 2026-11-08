"""커넥터 설정 린트. CI 에서 Terraform 이 렌더링할 것과 같은 결과를 만들어 규칙을 검사한다.

잡는 것
- 운영 사고로 이어지는 설정 누락(RDS 에서 락 스냅샷, 평문 암호, publication 자동 생성, 하트비트 없음)
- 토픽 누락: auto.create.topics.enable=false 라서 커넥터가 쓸 토픽이 topics.yaml 에 없으면 태스크가 멈춘다
  (테이블 토픽, 하트비트 토픽, 스키마 이력 토픽, outbox 라우팅 대상)
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from p1.streaming.topics import load_specs

CONNECTOR_DIR = Path(__file__).with_name("connectors")
OUTBOX_AGGREGATE_TYPES = ["payment"]  # outbox_events.aggregatetype 의 값(생성기·결제 서비스 계약)

SAMPLE_VARS = {
    "mysql_host": "danbi-dev-mysql.xxxx.ap-northeast-2.rds.amazonaws.com",
    "pg_host": "danbi-dev-postgres.xxxx.ap-northeast-2.rds.amazonaws.com",
    "secret_prefix": "danbi-dev/p1/",
    "bootstrap": "b-1.danbidevmsk.kafka.ap-northeast-2.amazonaws.com:9098",
    "schema_registry_url": "http://schema-registry.danbi.internal:8081",
    "region": "ap-northeast-2",
}


def render_template(text: str, variables: dict[str, str]) -> str:
    """Terraform templatefile 의 부분 구현: ${var} 치환, $${ 는 리터럴 ${ (뒤따르는 ${var} 는 계속 치환)."""
    out: list[str] = []
    i = 0
    while i < len(text):
        if text.startswith("$${", i):
            out.append("${")
            i += 3
        elif text.startswith("${", i):
            j = text.index("}", i)
            name = text[i + 2 : j]
            if name not in variables:
                raise KeyError(f"템플릿 변수 없음: {name}")
            out.append(variables[name])
            i = j + 1
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def load_connector(name: str, variables: dict[str, str] | None = None) -> dict[str, str]:
    text = (CONNECTOR_DIR / f"{name}.json.tftpl").read_text(encoding="utf-8")
    return json.loads(render_template(text, variables or SAMPLE_VARS))


COMMON_RULES = {
    "tombstones.on.delete": "false",  # Bronze 는 'd' 이벤트로 삭제를 받는다. 압축 토픽이 아니므로 툼스톤 불필요
    "decimal.handling.mode": "string",  # 금액 정밀도 보존(Avro bytes decimal 은 Python 소비자에서 다루기 어렵다)
    "event.processing.failure.handling.mode": "fail",  # 조용히 건너뛰면 유실이다
    "signal.enabled.channels": "source",
}
RULES = {
    "io.debezium.connector.mysql.MySqlConnector": {
        "snapshot.locking.mode": "none",  # RDS 는 FLUSH TABLES WITH READ LOCK 불가, 운영 테이블 락 금지
        "database.ssl.mode": "required",
        "include.schema.changes": "false",
    },
    "io.debezium.connector.postgresql.PostgresConnector": {
        "plugin.name": "pgoutput",
        "publication.autocreate.mode": "disabled",  # 슈퍼유저 없이, 명시한 테이블만
        "slot.drop.on.stop": "false",  # 재시작 때 슬롯이 사라지면 그 사이 변경을 잃는다
        "database.sslmode": "require",
    },
}


def expected_topics(cfg: dict[str, str]) -> set[str]:
    prefix = cfg["topic.prefix"]
    topics = set()
    outbox_pattern = None
    if "outbox" in cfg.get("transforms", "").split(","):
        outbox_pattern = re.compile(cfg.get("predicates.isOutbox.pattern", "$^"))
    for t in cfg["table.include.list"].split(","):
        topic = f"{prefix}.{t.strip()}"
        if outbox_pattern and outbox_pattern.fullmatch(topic):
            repl = cfg["transforms.outbox.route.topic.replacement"]
            topics |= {repl.replace("${routedByValue}", a) for a in OUTBOX_AGGREGATE_TYPES}
        else:
            topics.add(topic)
    if int(cfg.get("heartbeat.interval.ms", "0")) > 0:
        topics.add(f"{cfg.get('topic.heartbeat.prefix', '__debezium-heartbeat')}.{prefix}")
    if "schema.history.internal.kafka.topic" in cfg:
        topics.add(cfg["schema.history.internal.kafka.topic"])
    return topics


def lint(name: str, cfg: dict[str, str], known_topics: set[str]) -> list[str]:
    errs = []
    rules = {**COMMON_RULES, **RULES.get(cfg["connector.class"], {})}
    for k, v in rules.items():
        if cfg.get(k) != v:
            errs.append(f"{name}: {k}={cfg.get(k)!r}, 기대값 {v!r}")
    for k in ("database.user", "database.password"):
        if not cfg.get(k, "").startswith("${secretsmanager:"):
            errs.append(f"{name}: {k} 는 config provider(${{secretsmanager:...}})로만 넣는다")
    if cfg["connector.class"].endswith("PostgresConnector") and "heartbeat.action.query" not in cfg:
        errs.append(f"{name}: PG 는 heartbeat.action.query 가 없으면 한가한 DB 에서 슬롯이 WAL 을 붙잡는다")
    signal = cfg.get("signal.data.collection")
    if signal and signal not in cfg["table.include.list"].split(","):
        errs.append(
            f"{name}: 신호 테이블 {signal} 이 table.include.list 에 없으면 증분 스냅샷이 동작하지 않는다"
        )
    for t in sorted(expected_topics(cfg) - known_topics):
        errs.append(f"{name}: 토픽 {t} 이 topics.yaml 에 없다(auto.create 꺼짐 → 태스크 실패)")
    return errs


def main() -> int:
    known = {s.name for s in load_specs()}
    errs = []
    for p in sorted(CONNECTOR_DIR.glob("*.json.tftpl")):
        name = p.name.removesuffix(".json.tftpl")
        errs += lint(name, load_connector(name), known)
    print("\n".join(errs) or "ok")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
