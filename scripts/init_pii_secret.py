#!/usr/bin/env python3
"""PII 토큰화 HMAC 키를 생성해 Secrets Manager 에 넣는다.

키 값을 Terraform 으로 만들면 state 파일에 평문으로 남는다. 그래서 비밀 '틀'만 Terraform 이 만들고,
값은 이 스크립트가 만든다. 이미 값이 있으면 덮어쓰지 않는다(토큰이 바뀌면 조인이 깨진다).

    python scripts/init_pii_secret.py --env dev
"""

from __future__ import annotations

import argparse
import base64
import json
import secrets

import boto3


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--project", default="danbi")
    p.add_argument("--env", default="dev")
    p.add_argument("--region", default="ap-northeast-2")
    p.add_argument(
        "--rotate",
        action="store_true",
        help="새 버전을 만든다. 기존 토큰과 조인이 끊기므로 재처리 계획이 있을 때만",
    )
    args = p.parse_args()

    sm = boto3.client("secretsmanager", region_name=args.region)
    secret_id = f"{args.project}-{args.env}/pii/hmac-key"
    try:
        sm.get_secret_value(SecretId=secret_id)
        if not args.rotate:
            print(f"{secret_id}: 이미 값이 있다. 바꾸려면 --rotate")
            return
    except sm.exceptions.ResourceNotFoundException:
        pass

    key = base64.b64encode(secrets.token_bytes(32)).decode()
    version = secrets.token_hex(4)
    sm.put_secret_value(SecretId=secret_id, SecretString=json.dumps({"key_b64": key, "key_version": version}))
    print(f"{secret_id}: 키 버전 {version} 저장")


if __name__ == "__main__":
    main()
