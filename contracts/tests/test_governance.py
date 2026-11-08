import copy
import json

from contracts.compat import avro_compat, contract_compat
from contracts.governance import check, source_columns
from contracts.registry import ROOT

CLICK = json.loads((ROOT / "avro" / "click_event.avsc").read_text(encoding="utf-8"))


def test_repository_passes_governance():
    assert check() == []


def test_every_source_table_discovered():
    cols = source_columns()
    assert {"commerce.members", "payment.payments", "commerce.cs_tickets"} <= set(cols)
    assert "content" in cols["commerce.cs_tickets"]


def test_add_field_with_default_is_full_compatible():
    new = copy.deepcopy(CLICK)
    new["fields"].append({"name": "campaign", "type": ["null", "string"], "default": None})
    assert avro_compat(CLICK, new, "FULL") == []


def test_add_field_without_default_breaks_backward():
    new = copy.deepcopy(CLICK)
    new["fields"].append({"name": "campaign", "type": "string"})
    errs = avro_compat(CLICK, new, "BACKWARD")
    assert errs and "기본값" in errs[0]


def test_type_change_and_promotion():
    old = {
        "type": "record",
        "name": "R",
        "fields": [{"name": "n", "type": "int"}, {"name": "s", "type": "string"}],
    }
    promoted = {
        "type": "record",
        "name": "R",
        "fields": [{"name": "n", "type": "long"}, {"name": "s", "type": "string"}],
    }
    assert avro_compat(old, promoted, "BACKWARD") == []  # int → long 승격은 BACKWARD 호환
    assert avro_compat(old, promoted, "FORWARD")  # 옛 reader(int)는 long 을 못 읽는다
    broken = {
        "type": "record",
        "name": "R",
        "fields": [{"name": "n", "type": "string"}, {"name": "s", "type": "string"}],
    }
    assert avro_compat(old, broken, "BACKWARD")


def test_removing_field_without_default_breaks_forward_only():
    old = {
        "type": "record",
        "name": "R",
        "fields": [{"name": "a", "type": "int"}, {"name": "b", "type": "int"}],
    }
    new = {"type": "record", "name": "R", "fields": [{"name": "a", "type": "int"}]}
    assert avro_compat(old, new, "BACKWARD") == []
    assert avro_compat(old, new, "FORWARD")


def test_contract_breaking_changes():
    old = {"dataset": "silver.x", "grain": ["id"], "columns": [
        {"name": "id", "type": "bigint", "classification": "internal"},
        {"name": "amt", "type": "decimal(12,0)", "classification": "internal"},
        {"name": "m", "type": "string", "classification": "personal_token"}]}  # fmt: skip
    new = copy.deepcopy(old)
    new["columns"][1]["type"] = "string"
    new["columns"][2]["classification"] = "internal"
    new["columns"].append({"name": "extra", "type": "int", "classification": "internal"})
    errs = contract_compat(old, new)
    assert any("타입 변경" in e for e in errs) and any("분류 완화" in e for e in errs)
    del new["columns"][0]
    assert any("컬럼 삭제" in e for e in contract_compat(old, new))
    assert contract_compat(old, old) == []
