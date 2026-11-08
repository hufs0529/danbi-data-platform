import datetime as dt
import json
from pathlib import Path

import pytest

from contracts.registry import pii_policy
from danbi_common.pii import crypto
from danbi_common.pii.keystore import LocalKeyStore
from danbi_common.pii.tokenize import Tokenizer
from p1.streaming.ingest.pii import PiiTransformer
from p1.streaming.ingest.records import Decoder, change_seq
from p1.streaming.ingest.wire import SchemaCache, WireError, decode, encode

TOK = Tokenizer(b"k" * 32, "v1")

MYSQL_SOURCE = {
    "type": "record", "name": "Source", "namespace": "io.debezium.connector.mysql",
    "fields": [{"name": n, "type": ["null", t], "default": None} for n, t in
               [("connector", "string"), ("db", "string"), ("table", "string"), ("ts_ms", "long"),
                ("file", "string"), ("pos", "long"), ("row", "int"), ("gtid", "string"), ("snapshot", "string")]],
}  # fmt: skip
MEMBER_VALUE = {
    "type": "record", "name": "Value", "namespace": "cdc.mysql.commerce.members",
    "fields": [
        {"name": "member_id", "type": "long"}, {"name": "email", "type": "string"}, {"name": "name", "type": "string"},
        {"name": "phone", "type": "string"}, {"name": "birth_date", "type": ["null", "int"], "default": None},
        {"name": "grade", "type": "string"},
    ],
}  # fmt: skip
ENVELOPE = {
    "type": "record", "name": "Envelope", "namespace": "cdc.mysql.commerce.members",
    "fields": [
        {"name": "before", "type": ["null", MEMBER_VALUE], "default": None},
        {"name": "after", "type": ["null", "cdc.mysql.commerce.members.Value"], "default": None},
        {"name": "source", "type": MYSQL_SOURCE}, {"name": "op", "type": "string"},
        {"name": "ts_ms", "type": ["null", "long"], "default": None},
    ],
}  # fmt: skip
KEY = {"type": "record", "name": "Key", "namespace": "cdc.mysql.commerce.members",
       "fields": [{"name": "member_id", "type": "long"}]}  # fmt: skip
SCHEMAS = {1: ENVELOPE, 2: KEY}


@pytest.fixture
def decoder():
    cache = SchemaCache(lambda i: json.dumps(SCHEMAS[i]))
    return Decoder(cache, PiiTransformer(pii_policy(), TOK, LocalKeyStore()))


def _member(member_id=7, grade="GOLD"):
    return {"member_id": member_id, "email": "Kim@Example.com", "name": "김단비", "phone": "010-1234-5678",
            "birth_date": 10957, "grade": grade}  # fmt: skip


def _msg(op="u", before=None, after=None, pos=100, row=0, snapshot="false"):
    src = {"connector": "mysql", "db": "commerce", "table": "members", "ts_ms": 1_761_700_000_000,
           "file": "mysql-bin-changelog.000123", "pos": pos, "row": row, "gtid": "uuid:42", "snapshot": snapshot}  # fmt: skip
    value = encode({"before": before, "after": after, "source": src, "op": op, "ts_ms": 1}, ENVELOPE, 1)
    key = encode({"member_id": (after or before)["member_id"]}, KEY, 2)
    return key, value


def test_cdc_row_has_no_plaintext_pii(decoder):
    key, value = _msg(before=_member(grade="SILVER"), after=_member())
    row = decoder.cdc("cdc.mysql.commerce.members", key, value)
    blob = json.dumps(row, default=str)
    for plain in ("김단비", "010-1234-5678", "Kim@Example.com", "kim@example.com"):
        assert plain not in blob
    after = json.loads(row["after_json"])
    assert after["member_id"] == TOK.token("member", "7")
    assert json.loads(row["key_json"])["member_id"] == after["member_id"]  # 키도 같은 토큰 → 조인 가능
    assert after["email"] == TOK.token("email", "kim@example.com")
    assert after["name"].startswith(crypto.PREFIX)
    assert "birth_date" not in after and after["birth_date__birth_year"] == 2000
    assert row["op"] == "u" and row["txn_id"] == "uuid:42" and row["source_ts"].tzinfo is None


def test_encrypted_name_decrypts_with_member_key_and_shreds(decoder):
    key, value = _msg(op="c", after=_member())
    after = json.loads(decoder.cdc("cdc.mysql.commerce.members", key, value)["after_json"])
    store = decoder.pii.keys
    token = TOK.token("member", "7")
    assert crypto.decrypt(store.get(token), token, "name", after["name"]) == "김단비"
    store.shred(token, "req-1")
    key, value = _msg(op="u", after=_member())
    after2 = json.loads(decoder.cdc("cdc.mysql.commerce.members", key, value)["after_json"])
    assert after2["name"] == crypto.SHREDDED  # 파기 후 들어온 변경은 새 키를 만들지 않는다


def test_change_seq_orders_by_binlog_position_and_snapshot_first():
    s = {"connector": "mysql", "file": "mysql-bin-changelog.000123", "pos": 500, "row": 1}
    later = {**s, "pos": 900, "row": 0}
    assert change_seq(s, "u") < change_seq(later, "u")
    assert change_seq(s, "r") < change_seq(s, "u")
    next_file = {**s, "file": "mysql-bin-changelog.000124", "pos": 4}
    assert change_seq(later, "u") < change_seq(next_file, "u")
    assert change_seq({"connector": "postgresql", "lsn": 99}, "u") < change_seq(
        {"connector": "postgresql", "lsn": 100}, "c"
    )


def test_delete_has_only_before(decoder):
    key, value = _msg(op="d", before=_member())
    row = decoder.cdc("cdc.mysql.commerce.members", key, value)
    assert row["after_json"] is None and json.loads(row["before_json"])["member_id"].startswith("member_v1_")


def test_poison_pill_raises_wire_error(decoder):
    with pytest.raises(WireError, match="매직"):
        decoder.cdc("cdc.mysql.commerce.members", None, b"\x01garbage")
    with pytest.raises(WireError, match="짧다"):
        decode(b"\x00\x00", decoder.cache)


def test_signal_table_skipped(decoder):
    src = {"connector": "mysql", "db": "commerce", "table": "debezium_signal", "ts_ms": 1, "file": "f.1", "pos": 1,
           "row": 0, "gtid": None, "snapshot": "false"}  # fmt: skip
    value = encode({"before": None, "after": None, "source": src, "op": "c", "ts_ms": 1}, ENVELOPE, 1)
    assert decoder.cdc("cdc.mysql.commerce.debezium_signal", None, value) is None


CLICK_SCHEMA = json.loads(
    (Path(__file__).resolve().parents[3] / "contracts" / "avro" / "click_event.avsc").read_text(
        encoding="utf-8"
    )
)


def test_click_tokenizes_and_masks_search(decoder):
    decoder.cache = SchemaCache(lambda i: json.dumps(CLICK_SCHEMA))
    t = dt.datetime(2026, 10, 29, 1, 0, tzinfo=dt.UTC)
    event = {"event_id": "e1", "event_type": "search", "session_id": "s1", "member_id": 7, "device_id": "dev-1",
             "platform": "ios", "page": "/search", "product_id": None, "search_query": "010-9999-8888",
             "order_no": None, "user_agent": "ua", "event_time": t, "sent_time": t + dt.timedelta(hours=2)}  # fmt: skip
    row = decoder.click(encode(event, CLICK_SCHEMA, 5))
    assert row["member_id"] == TOK.token("member", "7")
    assert row["device_id"] == TOK.token("device", "dev-1")
    assert row["search_query"] == "[전화번호]"
    assert row["sent_time"] - row["event_time"] == dt.timedelta(hours=2) and row["schema_id"] == 5
