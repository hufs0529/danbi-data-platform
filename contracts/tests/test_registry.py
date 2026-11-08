import pytest

from contracts.registry import Column, Contract, athena_ddl, get, load_all, pii_policy, validate


def test_all_contracts_valid():
    contracts = load_all()
    assert len(contracts) >= 16
    errors = [e for c in contracts for e in validate(c)]
    assert errors == []


def test_pii_policy_covers_members_and_never_leaves_plaintext():
    policy = pii_policy()
    members = policy["commerce.members"]
    assert members["member_id"] == [{"op": "token", "arg": "member", "out": "member_id"}]
    # birth_date 는 일반화만 있으므로 원문 필드는 drop 이 붙어야 한다
    assert members["birth_date"][-1]["op"] == "drop"
    assert members["birth_date"][0] == {
        "op": "generalize",
        "arg": "birth_year",
        "out": "birth_date__birth_year",
    }
    for fields in policy.values():
        for ops in fields.values():
            assert ops[-1]["op"] in {"token", "encrypt", "mask", "drop"}


def test_address_generalize_before_encrypt():
    ops = pii_policy()["commerce.member_addresses"]["address1"]
    assert [o["op"] for o in ops] == ["generalize", "generalize", "encrypt"]
    assert {o["out"] for o in ops[:2]} == {"address1__sido", "address1__sigungu"}


def test_topics_in_policy():
    policy = pii_policy()
    assert policy["topic:clicks.raw"]["search_query"][-1]["op"] == "mask"
    assert policy["topic:events.payment"]["member_id"][-1]["op"] == "token"


def test_conflicting_replacing_ops_rejected():
    a = Contract("silver.a", "o", "d", ("x",), {"db": "d", "table": "t"}, "merge",
                 (Column("x", "string", "personal_token", "f", "token:member"),))  # fmt: skip
    b = Contract("silver.b", "o", "d", ("x",), {"db": "d", "table": "t"}, "merge",
                 (Column("x", "string", "personal_encrypted", "f", "encrypt:member"),))  # fmt: skip
    with pytest.raises(ValueError, match="서로 다른 치환"):
        pii_policy((a, b))


def test_validate_catches_token_with_internal_classification():
    bad = Contract("silver.a", "o", "d", ("x",), {"db": "d", "table": "t"}, "merge",
                   (Column("x", "string", "internal", "f", "token:member"),))  # fmt: skip
    assert any("personal_" in e for e in validate(bad))


def test_athena_ddl_shape():
    ddl = athena_ddl(get("silver.orders"), "danbi-dev-silver-123")
    assert ddl.startswith("CREATE TABLE IF NOT EXISTS silver.orders (")
    assert "PARTITIONED BY (month(ordered_at))" in ddl
    assert "LOCATION 's3://danbi-dev-silver-123/orders/'" in ddl
    assert "_src_seq string" in ddl and "'table_type'='ICEBERG'" in ddl
    scd2 = athena_ddl(get("silver.dim_member_scd2"), "b")
    assert "valid_from timestamp" in scd2 and "is_current boolean" in scd2
