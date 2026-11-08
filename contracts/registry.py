"""데이터 계약 로더와 생성기. 계약 YAML 하나에서 DDL·PII 정책·Silver 매핑을 만든다(README 참고)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).parent

INGEST_OPS = {"token", "encrypt", "mask", "generalize", "drop"}  # Flink(수집 시점)
SILVER_OPS = {"ts_iso", "ts_kst_local_ms", "date_days"}  # Spark(Silver)
REPLACING_OPS = {"token", "encrypt", "mask", "drop"}  # 원문 필드를 덮어쓰는 연산
GENERALIZERS = {"birth_year", "zip3", "sido", "sigungu"}
CLASSIFICATIONS = {
    "public",
    "internal",
    "personal_token",
    "personal_encrypted",
    "personal_generalized",
    "restricted",
}
TYPE_RE = re.compile(r"^(bigint|int|string|boolean|date|timestamp|double|decimal\(\d+,\d+\))$")

META_COLUMNS = {
    "merge": [("is_deleted", "boolean"), ("deleted_at", "timestamp"), ("_src_seq", "string"),
              ("_src_ts", "timestamp"), ("_ingested_at", "timestamp"), ("_run_id", "string")],
    "scd2": [("valid_from", "timestamp"), ("valid_to", "timestamp"), ("is_current", "boolean"),
             ("_src_seq", "string"), ("_ingested_at", "timestamp"), ("_run_id", "string")],
    "append": [("_ingested_at", "timestamp"), ("_run_id", "string")],
    "derived": [("_run_id", "string")],
}  # fmt: skip


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    classification: str
    source: str | None = None
    transform: str | None = None
    derive: str | None = None
    description: str = ""
    accepted_values: tuple[str, ...] = ()

    @property
    def op(self) -> tuple[str | None, str | None]:
        if not self.transform:
            return None, None
        op, _, arg = self.transform.partition(":")
        return op, (arg or None)

    @property
    def bronze_field(self) -> str | None:
        """Bronze JSON 에서 읽을 필드명(일반화는 파생 필드)."""
        op, arg = self.op
        if self.source is None:
            return None
        return f"{self.source}__{arg}" if op == "generalize" else self.source


@dataclass(frozen=True)
class Contract:
    dataset: str
    owner: str
    description: str
    grain: tuple[str, ...]
    source: dict[str, str]
    update: str
    columns: tuple[Column, ...]
    partition: tuple[str, ...] = ()
    retention: dict[str, Any] = field(default_factory=dict)
    sla: dict[str, Any] = field(default_factory=dict)
    scd2_tracked: tuple[str, ...] = ()
    quality: tuple[dict, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)

    @property
    def layer(self) -> str:
        return self.dataset.split(".")[0]

    @property
    def table(self) -> str:
        return self.dataset.split(".")[1]

    @property
    def source_key(self) -> str | None:
        """PII 정책의 키: CDC 는 '<db>.<table>', 토픽은 'topic:<name>'."""
        if "db" in self.source:
            return f"{self.source['db']}.{self.source['table']}"
        if "topic" in self.source:
            return f"topic:{self.source['topic']}"
        return None


def _parse(text: str) -> Contract | None:
    d = yaml.safe_load(text)
    if not isinstance(d, dict) or "dataset" not in d:
        return None  # 계약이 아닌 보조 파일(unmapped.yaml 등)
    cols = tuple(
        Column(c["name"], c["type"], c["classification"], c.get("source"), c.get("transform"), c.get("derive"),
               c.get("description", ""), tuple(c.get("accepted_values", [])))
        for c in d["columns"]
    )  # fmt: skip
    return Contract(d["dataset"], d["owner"], d["description"], tuple(d["grain"]), d.get("source", {}), d["update"],
                    cols, tuple(d.get("partition", [])), d.get("retention", {}), d.get("sla", {}),
                    tuple(d.get("scd2_tracked", [])), tuple(d.get("quality", [])), d)  # fmt: skip


def _yaml_files(node) -> list:
    out = []
    for child in node.iterdir():
        if child.is_dir():
            out += _yaml_files(child)
        elif child.name.endswith(".yaml"):
            out.append(child)
    return out


@cache
def load_all(root: Path | None = None) -> tuple[Contract, ...]:
    """계약 전부. Spark 잡은 zip(--py-files) 안에서 돌므로 파일 시스템 대신 importlib.resources 로 읽는다."""
    if root is not None:
        parsed = (_parse(p.read_text(encoding="utf-8")) for p in sorted(root.glob("p*/**/*.yaml")))
        return tuple(c for c in parsed if c is not None)
    from importlib import resources

    base = resources.files("contracts")
    files = sorted((f for d in base.iterdir() if d.is_dir() and d.name.startswith("p") for f in _yaml_files(d)),
                   key=lambda f: str(f))  # fmt: skip
    return tuple(c for c in (_parse(f.read_text(encoding="utf-8")) for f in files) if c is not None)


def get(dataset: str) -> Contract:
    for c in load_all():
        if c.dataset == dataset:
            return c
    raise KeyError(dataset)


# ---------------------------------------------------------------------------
# 구조 검증 (P1-12 에서 거버넌스 규칙이 더해진다)
# ---------------------------------------------------------------------------
def validate(c: Contract) -> list[str]:
    errs = []
    names = [col.name for col in c.columns]
    if len(names) != len(set(names)):
        errs.append(f"{c.dataset}: 컬럼 이름 중복")
    if c.update not in META_COLUMNS:
        errs.append(f"{c.dataset}: update={c.update}")
    for g in c.grain:
        if g not in names and g not in dict(META_COLUMNS.get(c.update, [])):
            errs.append(f"{c.dataset}: grain {g} 이 컬럼에 없다")
    if c.update == "scd2" and not c.scd2_tracked:
        errs.append(f"{c.dataset}: scd2 는 scd2_tracked 가 필요하다")
    for col in c.columns:
        if not TYPE_RE.match(col.type):
            errs.append(f"{c.dataset}.{col.name}: 지원하지 않는 타입 {col.type}")
        if col.classification not in CLASSIFICATIONS:
            errs.append(f"{c.dataset}.{col.name}: 분류 {col.classification}")
        op, arg = col.op
        if op and op not in INGEST_OPS | SILVER_OPS:
            errs.append(f"{c.dataset}.{col.name}: transform {col.transform}")
        if op == "generalize" and arg not in GENERALIZERS:
            errs.append(f"{c.dataset}.{col.name}: generalize:{arg}")
        if col.source is None and col.derive is None and c.update != "derived":
            errs.append(f"{c.dataset}.{col.name}: source 또는 derive 가 필요하다")
        if op in {"token", "encrypt"} and not col.classification.startswith("personal"):
            errs.append(f"{c.dataset}.{col.name}: {op} 컬럼은 personal_* 분류여야 한다")
    return errs


# ---------------------------------------------------------------------------
# 생성기
# ---------------------------------------------------------------------------
def pii_policy(contracts: tuple[Contract, ...] | None = None) -> dict[str, dict[str, list[dict[str, str]]]]:
    """Flink 수집 잡이 쓰는 정책: {소스: {필드: [연산...]}}. 일반화 → 치환 순서. 원문이 남을 필드는 drop 추가."""
    contracts = contracts or load_all()
    policy: dict[str, dict[str, list[dict[str, str]]]] = {}
    for c in contracts:
        key = c.source_key
        if key is None:
            continue
        for col in c.columns:
            op, arg = col.op
            if op not in INGEST_OPS or col.source is None:
                continue
            ops = policy.setdefault(key, {}).setdefault(col.source, [])
            entry = {"op": op, "arg": arg or "", "out": col.bronze_field or col.source}
            if entry not in ops:
                ops.append(entry)
    for fields in policy.values():
        for fname, ops in fields.items():
            replacing = [o for o in ops if o["op"] in REPLACING_OPS]
            if len({(o["op"], o["arg"]) for o in replacing}) > 1:
                raise ValueError(f"{fname}: 서로 다른 치환 연산 {replacing}")
            gens = sorted((o for o in ops if o["op"] == "generalize"), key=lambda o: o["arg"])
            tail = replacing[:1] or [{"op": "drop", "arg": "", "out": fname}]
            fields[fname] = gens + tail
    return policy


def silver_columns(c: Contract) -> list[tuple[str, str]]:
    cols = [(col.name, col.type) for col in c.columns]
    return cols + META_COLUMNS[c.update]


def athena_ddl(c: Contract, bucket: str) -> str:
    def esc(s: str) -> str:
        return s.replace("'", "''")

    cols = []
    for col in c.columns:
        comment = f" COMMENT '{esc(col.description or col.classification)}'"
        cols.append(f"  {col.name} {col.type}{comment}")
    cols += [f"  {n} {t}" for n, t in META_COLUMNS[c.update]]
    part = f"\nPARTITIONED BY ({', '.join(c.partition)})" if c.partition else ""
    return (
        f"CREATE TABLE IF NOT EXISTS {c.dataset} (\n" + ",\n".join(cols) + f"\n){part}\n"
        f"LOCATION 's3://{bucket}/{c.table}/'\n"
        "TBLPROPERTIES ('table_type'='ICEBERG', 'format'='parquet', 'write_compression'='zstd', "
        "'vacuum_max_snapshot_age_seconds'='604800')"
    )
