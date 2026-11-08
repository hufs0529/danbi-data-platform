"""생성기가 만드는 '할 일'. 시뮬레이터는 순수 함수로 액션을 만들고, 싱크가 실제 DB·Kafka·S3 에 적용한다.

이렇게 나누면 상태 기계 로직을 DB 없이 테스트할 수 있고, 같은 시나리오를 재현(seed)할 수 있다.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Literal

Db = Literal["commerce", "payment"]


@dataclass(frozen=True)
class Op:
    kind: Literal["insert", "update", "delete"]
    table: str
    values: dict[str, Any] = field(default_factory=dict)  # insert/update 할 값
    key: dict[str, Any] = field(default_factory=dict)  # update/delete 대상


@dataclass(frozen=True)
class Write:
    """한 트랜잭션. 결제 상태 변경과 outbox INSERT 가 같은 Write 에 들어가는 것이 Transactional Outbox 다."""

    db: Db
    ops: tuple[Op, ...]
    at: dt.datetime


@dataclass(frozen=True)
class Click:
    event: dict[str, Any]
    deliver_at: dt.datetime  # 앱 오프라인이면 event_time 보다 한참 뒤에 전송된다(F-05)


@dataclass(frozen=True)
class Ddl:
    db: Db
    sql: str
    note: str


Action = Write | Click | Ddl
