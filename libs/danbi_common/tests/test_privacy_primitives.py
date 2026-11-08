import boto3
import pytest
from moto import mock_aws

from danbi_common.pii import crypto
from danbi_common.pii.generalize import birth_year, sido, sigungu, zip3
from danbi_common.pii.keystore import MemberKeyStore, Shredded


def test_generalizers():
    assert birth_year(10957) == 2000 and birth_year("1988-03-01") == 1988
    assert zip3("06236") == "062" and zip3("123") is None
    assert sido("서울특별시 강남구 테헤란로 1") == "서울특별시"
    assert sido("서울 강남구 테헤란로 1") == "서울특별시"
    assert sido("강원도 춘천시 중앙로 1") == "강원특별자치도"
    assert sigungu("경기도 성남시 분당구 판교역로 1") == "성남시 분당구"
    assert sigungu("서울특별시 강남구 테헤란로 1") == "강남구"
    assert sigungu("알수없는 주소") is None


def test_aes_gcm_binds_member_and_field():
    dek = b"\x01" * 32
    ct = crypto.encrypt(dek, "member_v1_a", "name", "김단비")
    assert crypto.decrypt(dek, "member_v1_a", "name", ct) == "김단비"
    with pytest.raises(Exception):  # noqa: B017 (InvalidTag)
        crypto.decrypt(dek, "member_v1_b", "name", ct)  # 다른 회원으로 옮겨 붙인 암호문
    with pytest.raises(Exception):  # noqa: B017
        crypto.decrypt(dek, "member_v1_a", "address1", ct)


@pytest.fixture
def store():
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="ap-northeast-2")
        table = ddb.create_table(
            TableName="keys", KeySchema=[{"AttributeName": "member_token", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "member_token", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )  # fmt: skip
        kms = boto3.client("kms", region_name="ap-northeast-2")
        key_id = kms.create_key()["KeyMetadata"]["KeyId"]
        yield MemberKeyStore(table, kms, key_id, cache_ttl_s=0)  # 캐시 없이 매번 DynamoDB 확인


def test_keystore_get_or_create_is_stable(store):
    a = store.get_or_create("member_v1_x")
    assert len(a) == 32 and store.get_or_create("member_v1_x") == a


def test_shred_leaves_tombstone_and_blocks_recreation(store):
    store.get_or_create("member_v1_y")
    store.shred("member_v1_y", "req-9")
    with pytest.raises(Shredded):
        store.get("member_v1_y")
    with pytest.raises(Shredded):
        store.get_or_create("member_v1_y")
