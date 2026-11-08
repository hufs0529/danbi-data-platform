"""계약 거버넌스 검사 (CI: python -m contracts.governance).

registry.validate 가 '구조'를 본다면, 여기서는 '규칙'을 본다.
1. 보존 클래스는 retention.yaml 에 있는 것만(법적 근거 없는 보존 금지)
2. 소스 컬럼 전수 매핑: 소스 DDL 의 모든 컬럼은 계약에 있거나 unmapped.yaml 에 이유와 함께 있어야 한다
3. 이름으로 보아 개인정보인 컬럼(전화·이메일·이름·주소·생일·카드·계좌 …)은 수집 시점 변환이 있어야 한다
4. 소유자·설명·SLA 필수, 소유 팀은 알려진 팀이어야 한다
"""

from __future__ import annotations

import fnmatch
import re
import sys

import yaml

from contracts.registry import INGEST_OPS, ROOT, Contract, load_all, validate

SOURCES_SQL = ROOT.parent / "p1" / "sources" / "sql"
TEAMS = {"team-commerce-data", "team-payment-data", "team-marketing-data", "team-cs-data", "team-finance-data",
         "team-data-platform"}  # fmt: skip
PII_NAME = re.compile(r"(phone|email|e_mail|name|address|addr|birth|zipcode|card|account|rrn|ssn|passport|"
                      r"device_id|recipient|member_id|content|search_query)", re.I)  # fmt: skip
SOURCE_DB_DIR = {"commerce": "mysql", "payment": "postgres"}

_CREATE = re.compile(r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\);", re.S)
_COL = re.compile(r"^\s*(\w+)\s+[A-Za-z]", re.M)
_SKIP = {"PRIMARY", "UNIQUE", "KEY", "CONSTRAINT", "INDEX", "FOREIGN"}


def source_columns() -> dict[str, set[str]]:
    """p1/sources/sql 의 DDL 에서 '<db>.<table>' → 컬럼 집합."""
    out: dict[str, set[str]] = {}
    for db, d in SOURCE_DB_DIR.items():
        for p in sorted((SOURCES_SQL / d).glob("*.sql")):
            text = re.sub(r"--[^\n]*", "", p.read_text(encoding="utf-8"))
            for table, body in _CREATE.findall(text):
                cols = {c for c in _COL.findall(body) if c.upper() not in _SKIP}
                out[f"{db}.{table}"] = cols
    return out


def load_unmapped() -> dict[str, str]:
    return yaml.safe_load((ROOT / "p1" / "unmapped.yaml").read_text(encoding="utf-8")) or {}


def load_retention() -> dict[str, dict]:
    return yaml.safe_load((ROOT / "retention.yaml").read_text(encoding="utf-8"))["classes"]


def check(contracts: tuple[Contract, ...] | None = None) -> list[str]:
    contracts = contracts or load_all()
    errs = [e for c in contracts for e in validate(c)]
    retention = load_retention()
    for c in contracts:
        if c.retention.get("class") not in retention:
            errs.append(
                f"{c.dataset}: 보존 클래스 {c.retention.get('class')} 는 retention.yaml 에 없다(법적 근거 필요)"
            )
        if c.owner not in TEAMS:
            errs.append(f"{c.dataset}: 알 수 없는 소유 팀 {c.owner}")
        if not c.description.strip() or not c.sla:
            errs.append(f"{c.dataset}: 설명·SLA 필수")

    # 소스 컬럼 전수 매핑
    mapped: dict[str, set[str]] = {}
    transformed: dict[str, set[str]] = {}
    for c in contracts:
        if c.source_key and "." in c.source_key and not c.source_key.startswith("topic:"):
            for col in c.columns:
                if col.source:
                    mapped.setdefault(c.source_key, set()).add(col.source)
                    if col.op[0] in INGEST_OPS:
                        transformed.setdefault(c.source_key, set()).add(col.source)
    unmapped = load_unmapped()
    for table, cols in source_columns().items():
        for col in sorted(cols):
            fq = f"{table}.{col}"
            if col in mapped.get(table, set()):
                if (
                    PII_NAME.search(col)
                    and col not in transformed.get(table, set())
                    and not _allowed_plain(table, col)
                ):
                    errs.append(
                        f"{fq}: 개인정보로 보이는데 수집 시점 변환(token/encrypt/mask/generalize/drop)이 없다"
                    )
                continue
            if not any(fnmatch.fnmatch(fq, pat) for pat in unmapped):
                errs.append(f"{fq}: 계약에도 unmapped.yaml 에도 없다(새 컬럼은 분류 후 계약에 추가)")
    return errs


def _allowed_plain(table: str, col: str) -> bool:
    """이름은 PII 처럼 보이지만 평문이 맞는 컬럼(검토 완료)."""
    return (table, col) in {("commerce.products", "name"), ("payment.payments", "card_issuer"),
                            ("commerce.member_addresses", "label"), ("commerce.member_addresses", "address_id"),
                            ("commerce.orders", "address_id")}  # fmt: skip


def main() -> int:
    errs = check()
    print("\n".join(errs) or f"ok: {len(load_all())} contracts")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
