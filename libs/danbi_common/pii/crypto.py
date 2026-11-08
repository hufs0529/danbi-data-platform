"""회원별 데이터 키로 필드를 암호화한다(crypto-shredding).

- 알고리즘: AES-256-GCM. AAD 에 (회원 토큰, 필드명)을 넣어 암호문을 다른 회원·다른 필드로 옮겨 붙일 수 없게 한다.
- 형식: enc:v1:<base64(nonce12 + ciphertext + tag)>
- 탈퇴 시 키를 폐기하면 Bronze·스냅샷·백업에 남은 암호문도 읽을 수 없게 된다. 이것이 불변 레이크에서의 '파기'다.
"""

from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PREFIX = "enc:v1:"
SHREDDED = "enc:shredded"  # 키가 이미 폐기된 회원의 값(복호화 불가 표시)


def _aad(member_token: str, field: str) -> bytes:
    return f"{member_token}|{field}".encode()


def encrypt(dek: bytes, member_token: str, field: str, plaintext: str) -> str:
    nonce = os.urandom(12)
    ct = AESGCM(dek).encrypt(nonce, plaintext.encode(), _aad(member_token, field))
    return PREFIX + base64.b64encode(nonce + ct).decode()


def decrypt(dek: bytes, member_token: str, field: str, token: str) -> str:
    if not token.startswith(PREFIX):
        raise ValueError("암호문 형식이 아니다")
    raw = base64.b64decode(token[len(PREFIX) :])
    return AESGCM(dek).decrypt(raw[:12], raw[12:], _aad(member_token, field)).decode()
