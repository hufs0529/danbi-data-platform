"""잡 실행 환경에서 Decoder 를 조립한다(비밀·키 저장소·스키마 레지스트리 연결).

Managed Flink 는 애플리케이션 속성을 /etc/flink/application_properties.json 으로 준다.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from danbi_common.pii.tokenize import Tokenizer

from .pii import PiiTransformer
from .records import Decoder
from .wire import SchemaCache, http_fetcher

PROPS_FILE = Path("/etc/flink/application_properties.json")
POLICY_FILE = Path(__file__).with_name("pii_policy.json")  # build_flink_app.sh 가 계약에서 생성


def app_properties(group: str = "app") -> dict[str, str]:
    if PROPS_FILE.exists():
        for g in json.loads(PROPS_FILE.read_text()):
            if g["PropertyGroupId"] == group:
                return g["PropertyMap"]
    return {k[4:].lower(): v for k, v in os.environ.items() if k.startswith("APP_")}  # 로컬 실행


def build_decoder(props: dict[str, str]) -> Decoder:
    import boto3

    from danbi_common.aws import get_secret_string
    from danbi_common.pii.keystore import MemberKeyStore

    region = props.get("region", "ap-northeast-2")
    tokenizer = Tokenizer.from_secret_string(get_secret_string(props["hmac_secret_id"]))
    keystore = MemberKeyStore(
        boto3.resource("dynamodb", region_name=region).Table(props["member_key_table"]),
        boto3.client("kms", region_name=region),
        props["kms_pii_arn"],
        cache_ttl_s=float(props.get("member_key_cache_ttl_s", "300")),
    )
    policy = json.loads(POLICY_FILE.read_text(encoding="utf-8"))
    return Decoder(
        SchemaCache(http_fetcher(props["schema_registry_url"])), PiiTransformer(policy, tokenizer, keystore)
    )
