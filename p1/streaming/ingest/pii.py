"""수집 시점 PII 처리. 정책은 계약에서 생성된다(contracts.registry.pii_policy → 빌드 시 pii_policy.json).

같은 레코드 안의 member_id(원문)로 회원 키를 찾아 암호화한다. 그래서 토큰화보다 먼저 원문 member_id 를 읽는다.
"""

from __future__ import annotations

from typing import Any

from danbi_common.pii import crypto
from danbi_common.pii.detect import mask
from danbi_common.pii.generalize import GENERALIZERS
from danbi_common.pii.keystore import Shredded
from danbi_common.pii.tokenize import Tokenizer

Policy = dict[str, dict[str, list[dict[str, str]]]]


class PiiTransformer:
    def __init__(
        self, policy: Policy, tokenizer: Tokenizer, keystore, member_field: str = "member_id"
    ) -> None:
        self.policy = policy
        self.tok = tokenizer
        self.keys = keystore
        self.member_field = member_field

    def has_rules(self, source_key: str) -> bool:
        return source_key in self.policy

    def apply(self, source_key: str, record: dict[str, Any] | None) -> dict[str, Any] | None:
        if record is None:
            return None
        rules = self.policy.get(source_key)
        if not rules:
            return record
        raw_member = record.get(self.member_field)
        member_token = self.tok.token("member", str(raw_member)) if raw_member is not None else None
        out = dict(record)
        for field, ops in rules.items():
            if field not in record:
                continue
            value = record[field]
            for op in ops:
                kind, arg, target = op["op"], op["arg"], op["out"]
                if kind == "generalize":
                    out[target] = GENERALIZERS[arg](value)
                elif kind == "token":
                    out[field] = None if value is None else self.tok.token(arg, str(value))
                elif kind == "mask":
                    out[field] = None if value is None else mask(str(value))
                elif kind == "drop":
                    out.pop(field, None)
                elif kind == "encrypt":
                    out[field] = self._encrypt(member_token, field, value)
        return out

    def _encrypt(self, member_token: str | None, field: str, value: Any) -> str | None:
        if value is None:
            return None
        if member_token is None:
            return None  # 누구의 키로 암호화할지 모르면 들이지 않는다
        try:
            dek = self.keys.get_or_create(member_token)
        except Shredded:
            return crypto.SHREDDED
        return crypto.encrypt(dek, member_token, field, str(value))
