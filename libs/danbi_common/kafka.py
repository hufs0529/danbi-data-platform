"""MSK(IAM 인증) 클라이언트 설정. confluent-kafka(librdkafka) 기준.

MSK IAM 인증은 SASL/OAUTHBEARER 로 동작한다. 토큰은 15분 유효하고 librdkafka 가 만료 전에 콜백을 다시 부른다.
포트는 9098(IAM). 9092(평문)·9094(TLS)는 클러스터에서 꺼 둔다.
"""

from __future__ import annotations

import os
import socket


def _oauth_cb(region: str):
    def cb(_config: str):
        from aws_msk_iam_sasl_signer import MSKAuthTokenProvider

        token, expiry_ms = MSKAuthTokenProvider.generate_auth_token(region)
        return token, expiry_ms / 1000

    return cb


def client_config(bootstrap: str | None = None, region: str | None = None, **extra) -> dict:
    region = region or os.environ.get("AWS_REGION", "ap-northeast-2")
    conf = {
        "bootstrap.servers": bootstrap or os.environ["MSK_BOOTSTRAP"],
        "security.protocol": "SASL_SSL",
        "sasl.mechanisms": "OAUTHBEARER",
        "oauth_cb": _oauth_cb(region),
        "client.id": os.environ.get("KAFKA_CLIENT_ID", socket.gethostname()),
    }
    conf.update(extra)
    return conf


def producer_config(bootstrap: str | None = None, **extra) -> dict:
    # 멱등 프로듀서: 재시도로 인한 중복·순서 뒤바뀜을 브로커가 막는다(acks=all 필수)
    base = {"enable.idempotence": True, "acks": "all", "linger.ms": 50, "compression.type": "zstd"}
    base.update(extra)
    return client_config(bootstrap, **base)
