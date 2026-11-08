"""회원별 데이터 키 저장소 (DynamoDB + KMS 봉투 암호화).

DynamoDB 항목: { member_token(PK), wrapped_dek(KMS 로 암호화된 DEK), key_version, status, created_at, shredded_at }
- get_or_create: 처음 보는 회원이면 KMS GenerateDataKey → 조건부 PutItem(동시 생성 경쟁 방지)
- shred: 키를 지우는 대신 status=SHREDDED 묘비를 남긴다. 지우면 늦게 온 이벤트가 새 키를 만들어
         '파기된 회원의 새 암호문'이 생기기 때문이다.
- PITR(시점 복구)를 켜지 않는다: 켜면 35일 동안 폐기한 키를 복구할 수 있어 파기가 완료되지 않는다.
- 평문 DEK 는 프로세스 메모리 캐시(TTL)에만 둔다. TTL 이 파기 반영 지연의 상한이 된다.
"""

from __future__ import annotations

import datetime as dt
import time
from collections import OrderedDict
from dataclasses import dataclass


class Shredded(Exception):
    """이미 폐기된 회원."""


@dataclass
class _Entry:
    dek: bytes | None
    at: float


class MemberKeyStore:
    def __init__(
        self, table, kms, kms_key_id: str, cache_ttl_s: float = 300, cache_size: int = 50_000
    ) -> None:
        self.table = table  # boto3 DynamoDB Table 리소스
        self.kms = kms
        self.kms_key_id = kms_key_id
        self.ttl = cache_ttl_s
        self.size = cache_size
        self._cache: OrderedDict[str, _Entry] = OrderedDict()

    def _cache_get(self, token: str) -> _Entry | None:
        e = self._cache.get(token)
        if e and time.monotonic() - e.at < self.ttl:
            self._cache.move_to_end(token)
            return e
        return None

    def _cache_put(self, token: str, dek: bytes | None) -> None:
        self._cache[token] = _Entry(dek, time.monotonic())
        self._cache.move_to_end(token)
        while len(self._cache) > self.size:
            self._cache.popitem(last=False)

    def _unwrap(self, item: dict) -> bytes:
        blob = item["wrapped_dek"]
        blob = blob.value if hasattr(blob, "value") else bytes(blob)
        ctx = {"member_token": item["member_token"]}
        return self.kms.decrypt(CiphertextBlob=blob, EncryptionContext=ctx)["Plaintext"]

    def get(self, token: str) -> bytes:
        """키를 돌려준다. 없으면 KeyError, 폐기됐으면 Shredded."""
        if (e := self._cache_get(token)) is not None:
            if e.dek is None:
                raise Shredded(token)
            return e.dek
        item = self.table.get_item(Key={"member_token": token}, ConsistentRead=True).get("Item")
        if item is None:
            raise KeyError(token)
        if item.get("status") == "SHREDDED":
            self._cache_put(token, None)
            raise Shredded(token)
        dek = self._unwrap(item)
        self._cache_put(token, dek)
        return dek

    def get_or_create(self, token: str) -> bytes:
        try:
            return self.get(token)
        except KeyError:
            pass
        # 암호화 컨텍스트에 회원 토큰을 묶어, 다른 회원의 항목으로 바꿔치기한 키는 KMS 가 풀어주지 않는다
        gen = self.kms.generate_data_key(
            KeyId=self.kms_key_id, KeySpec="AES_256", EncryptionContext={"member_token": token}
        )
        try:
            self.table.put_item(
                Item={"member_token": token, "wrapped_dek": gen["CiphertextBlob"], "key_version": 1,
                      "status": "ACTIVE", "created_at": dt.datetime.now(dt.UTC).isoformat()},
                ConditionExpression="attribute_not_exists(member_token)",
            )  # fmt: skip
        except self.table.meta.client.exceptions.ConditionalCheckFailedException:
            return self.get(token)  # 다른 작업자가 먼저 만들었다
        self._cache_put(token, gen["Plaintext"])
        return gen["Plaintext"]

    def shred(self, token: str, request_id: str) -> None:
        self.table.put_item(
            Item={"member_token": token, "status": "SHREDDED", "request_id": request_id,
                  "shredded_at": dt.datetime.now(dt.UTC).isoformat()}
        )  # fmt: skip
        self._cache_put(token, None)


class LocalKeyStore:
    """테스트·로컬 실행용."""

    def __init__(self) -> None:
        import os

        self._keys: dict[str, bytes | None] = {}
        self._rand = os.urandom

    def get(self, token: str) -> bytes:
        if token not in self._keys:
            raise KeyError(token)
        if self._keys[token] is None:
            raise Shredded(token)
        return self._keys[token]  # type: ignore[return-value]

    def get_or_create(self, token: str) -> bytes:
        if token not in self._keys:
            self._keys[token] = self._rand(32)
        return self.get(token)

    def shred(self, token: str, request_id: str) -> None:
        self._keys[token] = None
