import json
from pathlib import Path

import pytest

from contracts.registry import load_all
from p1.governance.lf_sync import TableTags, diff, from_contracts, from_manifest

MANIFEST = Path(__file__).resolve().parents[2] / "dbt" / "target" / "manifest.json"


def test_silver_tags_from_contracts():
    tags = {t.table: t for t in from_contracts(load_all())}
    m = tags["members"]
    assert m.table_tags == {"domain": "commerce", "classification": "internal"}
    assert m.column_tags["member_token"] == "personal_token"
    assert m.column_tags["name_enc"] == "personal_encrypted"
    assert "grade" not in m.column_tags  # 기본값(internal)은 테이블 태그를 상속
    assert tags["payments"].table_tags["domain"] == "payment"


def test_gold_tags_from_manifest_with_token_safety_net():
    manifest = {"nodes": {
        "model.danbi.fct_payments": {"resource_type": "model", "name": "fct_payments", "tags": ["publish"],
                                      "meta": {"owner": "team-payment-data"},
                                      "columns": {"member_token": {"meta": {}}, "net_amount": {"meta": {}}}},
        "model.danbi.stg_orders": {"resource_type": "model", "name": "stg_orders", "tags": []},
    }}  # fmt: skip
    [t, recon] = from_manifest(manifest)
    assert t.table == "fct_payments" and t.table_tags["domain"] == "payment"
    assert t.column_tags == {"member_token": "personal_token"}  # 메타가 없어도 _token 은 개인정보로
    assert recon.table == "recon_pg_settlement" and recon.table_tags["domain"] == "finance"


@pytest.mark.skipif(not MANIFEST.exists(), reason="dbt parse 결과 없음")
def test_real_manifest_marks_member_token():
    tags = {t.table: t for t in from_manifest(json.loads(MANIFEST.read_text()))}
    assert tags["fct_order_items"].column_tags.get("member_token") == "personal_token"
    assert tags["dim_member"].column_tags.get("member_token") == "personal_token"


def test_diff_sets_and_resets():
    d = TableTags(
        "silver", "t", {"domain": "commerce", "classification": "internal"}, {"m": "personal_token"}
    )
    acts = diff(d, {"domain": "payment"}, {"m": {}, "old": {"classification": "restricted"}})
    assert ("table_set", "t", "domain", "commerce") in acts
    assert ("table_set", "t", "classification", "internal") in acts
    assert ("column_set", "t.m", "classification", "personal_token") in acts
    assert ("column_reset", "t.old", "classification", "internal") in acts
    assert (
        diff(
            d,
            {"domain": "commerce", "classification": "internal"},
            {"m": {"classification": "personal_token"}},
        )
        == []
    )
