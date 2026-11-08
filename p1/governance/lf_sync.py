"""Lake Formation 태그 동기화(태그 as code). 계약(Silver)과 dbt manifest(Gold)가 원본이다.

    python -m p1.governance.lf_sync plan --manifest p1/dbt/target/manifest.json
    python -m p1.governance.lf_sync apply --manifest s3://<artifacts>/dbt/runs/<id>/manifest.json
    python -m p1.governance.lf_sync revoke-iam-allowed     # LF 전환 1회: IAMAllowedPrincipals 슈퍼 권한 회수

태그 키
  layer          : silver | gold | ops | gold_audit           (DB 단위)
  domain         : commerce | payment | marketing | cs | finance (테이블 단위, 계약 owner 에서)
  classification : public | internal | personal_token | personal_encrypted | personal_generalized | restricted
                   테이블 기본값 internal, 컬럼이 다르면 컬럼에 덮어쓴다(LF 는 컬럼 태그가 테이블 태그를 이긴다)
권한은 태그 식으로 준다(p1-60-governance). 분석가 = layer:gold ∧ classification ∈ {public, internal, personal_generalized}
→ member_token(personal_token) 컬럼은 분석가 쿼리 결과에서 자동으로 빠진다.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field

from contracts.registry import Contract, load_all

OWNER_DOMAIN = {"team-commerce-data": "commerce", "team-payment-data": "payment", "team-marketing-data": "marketing",
                "team-cs-data": "cs", "team-finance-data": "finance", "team-data-platform": "platform"}  # fmt: skip
DEFAULT_CLASS = "internal"
EXTRA_GOLD = {"recon_pg_settlement": "finance"}  # dbt 밖에서 만드는 gold 테이블의 도메인


@dataclass
class TableTags:
    database: str
    table: str
    table_tags: dict[str, str]
    column_tags: dict[str, str] = field(default_factory=dict)  # 컬럼 → classification(기본값과 다를 때만)


def from_contracts(contracts: tuple[Contract, ...]) -> list[TableTags]:
    out = []
    for c in contracts:
        if c.layer != "silver":
            continue
        cols = {col.name: col.classification for col in c.columns if col.classification != DEFAULT_CLASS}
        out.append(TableTags("silver", c.table, {"domain": OWNER_DOMAIN[c.owner], "classification": DEFAULT_CLASS},
                             cols))  # fmt: skip
    return out


def from_manifest(manifest: dict) -> list[TableTags]:
    out = []
    for node in manifest.get("nodes", {}).values():
        if node.get("resource_type") != "model" or "publish" not in node.get("tags", []):
            continue
        meta = node.get("meta", {}) or node.get("config", {}).get("meta", {})
        owner = meta.get("owner", "team-data-platform")
        cols = {}
        for name, col in node.get("columns", {}).items():
            cls = (col.get("meta") or {}).get("classification")
            if cls is None and name.endswith("_token"):
                cls = "personal_token"  # 이름 규칙 안전망: 토큰 컬럼은 메타가 없어도 개인정보로
            if cls and cls != DEFAULT_CLASS:
                cols[name] = cls
        out.append(TableTags("gold", node["name"], {"domain": OWNER_DOMAIN.get(owner, "platform"),
                                                     "classification": meta.get("classification", DEFAULT_CLASS)}, cols))  # fmt: skip
    for t, d in EXTRA_GOLD.items():
        out.append(TableTags("gold", t, {"domain": d, "classification": DEFAULT_CLASS}))
    return out


def diff(
    desired: TableTags, current_table: dict[str, str], current_cols: dict[str, dict[str, str]]
) -> list[tuple]:
    """(동작, 대상, 키, 값) 목록. 동작: table_set | column_set | column_reset"""
    actions = []
    for k, v in desired.table_tags.items():
        if current_table.get(k) != v:
            actions.append(("table_set", desired.table, k, v))
    for col, v in desired.column_tags.items():
        if current_cols.get(col, {}).get("classification") != v:
            actions.append(("column_set", f"{desired.table}.{col}", "classification", v))
    for col, tags in current_cols.items():
        if (
            "classification" in tags
            and col not in desired.column_tags
            and tags["classification"] != desired.table_tags.get("classification")
        ):
            actions.append(
                (
                    "column_reset",
                    f"{desired.table}.{col}",
                    "classification",
                    desired.table_tags["classification"],
                )
            )
    return actions


# ---------------------------------------------------------------------------
def _current(lf, t: TableTags):
    try:
        r = lf.get_resource_lf_tags(
            Resource={"Table": {"DatabaseName": t.database, "Name": t.table}}, ShowAssignedLFTags=True
        )
    except lf.exceptions.EntityNotFoundException:
        return None, None
    table = {x["TagKey"]: x["TagValues"][0] for x in r.get("LFTagsOnTable", [])}
    cols = {
        c["Name"]: {x["TagKey"]: x["TagValues"][0] for x in c.get("LFTags", [])}
        for c in r.get("LFTagsOnColumns", [])
    }
    return table, cols


def _apply(lf, t: TableTags, actions: list[tuple]) -> None:
    for act, target, key, value in actions:
        tag = [{"TagKey": key, "TagValues": [value]}]
        if act == "table_set":
            res = {"Table": {"DatabaseName": t.database, "Name": t.table}}
        else:
            col = target.split(".", 1)[1]
            res = {"TableWithColumns": {"DatabaseName": t.database, "Name": t.table, "ColumnNames": [col]}}
        lf.add_lf_tags_to_resource(Resource=res, LFTags=tag)


def _load_manifest(path: str) -> dict:
    if path.startswith("s3://"):
        import boto3

        b, k = path[5:].split("/", 1)
        return json.loads(boto3.client("s3").get_object(Bucket=b, Key=k)["Body"].read())
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def revoke_iam_allowed(lf, glue, databases=("silver", "gold", "ops", "gold_audit")) -> int:
    """LF 전환: 기본 IAMAllowedPrincipals 'ALL' 권한을 DB·테이블에서 회수해 LF 권한만 남긴다(되돌릴 수 있게 로그)."""
    n = 0
    principal = {"DataLakePrincipalIdentifier": "IAM_ALLOWED_PRINCIPALS"}
    for db in databases:
        resources = [{"Database": {"Name": db}}]
        for page in glue.get_paginator("get_tables").paginate(DatabaseName=db):
            resources += [{"Table": {"DatabaseName": db, "Name": t["Name"]}} for t in page["TableList"]]
        for res in resources:
            try:
                lf.revoke_permissions(Principal=principal, Resource=res, Permissions=["ALL"])
                print(f"revoked IAM_ALLOWED_PRINCIPALS on {json.dumps(res)}")
                n += 1
            except lf.exceptions.InvalidInputException:
                pass  # 이미 없음
    return n


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["plan", "apply", "revoke-iam-allowed"])
    p.add_argument("--manifest")
    args = p.parse_args(argv)
    import boto3

    lf = boto3.client("lakeformation")
    if args.action == "revoke-iam-allowed":
        print(f"{revoke_iam_allowed(lf, boto3.client('glue'))} resources")
        return 0
    desired = from_contracts(load_all()) + (
        from_manifest(_load_manifest(args.manifest)) if args.manifest else []
    )
    for t in desired:
        table, cols = _current(lf, t)
        if table is None:
            print(f"? {t.database}.{t.table}: 테이블 없음(아직 생성 전)")
            continue
        actions = diff(t, table, cols)
        for a in actions:
            print(f"~ {t.database}.{a[1]} {a[2]}={a[3]} ({a[0]})")
        if args.action == "apply" and actions:
            _apply(lf, t, actions)
    return 0


if __name__ == "__main__":
    sys.exit(main())
