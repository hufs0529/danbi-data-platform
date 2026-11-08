"""결정적 토큰화.

같은 값은 항상 같은 토큰이 되므로 레이크 안에서 조인·집계는 되지만, 키 없이는 원문으로 돌아갈 수 없다.
- 토큰에 종류와 키 버전을 넣는다: 키를 회전하면 버전이 바뀌어, 섞인 토큰을 탐지할 수 있다.
- 정규화를 먼저 한다: '010-1234-5678' 과 '+82 10 1234 5678' 은 같은 사람이다.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import unicodedata
from dataclasses import dataclass

_NON_DIGIT = re.compile(r"\D")
_SPACES = re.compile(r"\s+")

DIGIT_KINDS = {"phone", "card", "account", "rrn"}


def normalize(kind: str, value: str) -> str:
    v = unicodedata.normalize("NFC", value).strip()
    if kind == "phone":
        digits = _NON_DIGIT.sub("", v)
        if digits.startswith("82") and len(digits) >= 11:
            digits = "0" + digits[2:]
        return digits
    if kind in DIGIT_KINDS:
        return _NON_DIGIT.sub("", v)
    if kind == "email":
        return v.lower()
    if kind == "name":
        return _SPACES.sub("", v)
    if kind == "address":
        return _SPACES.sub(" ", v)
    return v


@dataclass(frozen=True)
class Tokenizer:
    key: bytes
    key_version: str

    @classmethod
    def from_secret_string(cls, secret_string: str) -> Tokenizer:
        """Secrets Manager 값 {"key_b64": ..., "key_version": ...} 로 만든다."""
        data = json.loads(secret_string)
        return cls(key=base64.b64decode(data["key_b64"]), key_version=str(data["key_version"]))

    def token(self, kind: str, value: str | None) -> str | None:
        if value is None:
            return None
        norm = normalize(kind, value)
        if not norm:
            return None
        mac = hmac.new(self.key, f"{kind}:{norm}".encode(), hashlib.sha256).digest()
        body = base64.b32encode(mac[:15]).decode().lower()
        return f"{kind}_{self.key_version}_{body}"

    @staticmethod
    def key_version_of(token: str) -> str | None:
        parts = token.split("_")
        return parts[1] if len(parts) == 3 else None
