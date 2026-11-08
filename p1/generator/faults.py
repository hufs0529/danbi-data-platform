"""결함 주입 설정. 장애 훈련(F-01~F-10)과 변경 관리 시나리오(S-C1)를 재현하는 손잡이."""

from __future__ import annotations

from dataclasses import dataclass, fields


@dataclass
class Faults:
    late_ratio: float = 0.0  # 클릭: 앱 오프라인 후 지연 전송(F-05)
    late_max_minutes: int = 120
    dup_ratio: float = 0.0  # 클릭·outbox: 같은 이벤트 재전송
    out_of_order_ratio: float = 0.0  # 결제: 매입 웹훅이 승인보다 먼저 도착(상태 역전)
    hard_delete_ratio: float = 0.0  # 배송지 하드 삭제
    bot_ratio: float = 0.0  # 봇 세션 비율
    pii_in_text_ratio: float = 0.1  # CS 문의·검색어에 개인정보가 섞이는 비율
    schema_change_at: int | None = None  # N번째 tick 에 orders 컬럼 추가(S-C1 호환 변경)
    breaking_change_at: int | None = None  # N번째 tick 에 타입 변경(S-C1 비호환 → CI·계약이 막아야 함)
    null_amount_ratio: float = 0.0  # 결제 금액 null 급증(F-08). 소스 제약이 없는 outbox payload 에만
    fraud_burst_ratio: float = 0.0  # 초당 확률: 한 카드로 5분 안에 서로 다른 배송지 3곳 이상(S-B3)

    @classmethod
    def parse(cls, spec: str | None) -> Faults:
        """'late=0.02,dup=0.01,schema_change_at=100' 형식."""
        f = cls()
        if not spec:
            return f
        alias = {"late": "late_ratio", "dup": "dup_ratio", "ooo": "out_of_order_ratio",
                 "hard_delete": "hard_delete_ratio", "bot": "bot_ratio", "null_amount": "null_amount_ratio", "fraud": "fraud_burst_ratio"}  # fmt: skip
        types = {fl.name: fl.type for fl in fields(cls)}
        for part in spec.split(","):
            k, _, v = part.partition("=")
            name = alias.get(k.strip(), k.strip())
            if name not in types:
                raise ValueError(f"알 수 없는 결함: {k}")
            setattr(f, name, int(v) if "int" in str(types[name]) else float(v))
        return f
