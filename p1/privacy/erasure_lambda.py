"""회원 탈퇴 파기 워크플로 단계 (Step Functions 가 단계마다 이 Lambda 를 부른다, S-G4). boto3 만 사용.

파기와 보존의 충돌을 이렇게 푼다
- 회원 자체 정보(회원·배송지·등급 이력·행태): 행 삭제
- 거래 사실(주문·결제·쿠폰 사용): 금액·일시는 남기고 회원 연결만 끊는다(member_token → 'erased')
  → 매출 지표가 과거로 소급해 바뀌지 않는다. 법정 보존(5년)용 원래 연결은 분리 보관소(retention)에만, 별도 키로
- CS 문의: 분쟁 처리 기록 3년 → 분리 보관 후 일반 영역에서는 본문 제거
- 암호문(이름·주소): 회원 키 폐기(crypto-shredding) → Bronze·스냅샷·백업 속 암호문까지 읽을 수 없다
- Bronze(토큰이 남은 원본 로그): 행 삭제. 이후 스냅샷 만료·고아 파일 정리(EMR)로 물리 삭제 완료
모든 단계는 멱등이다(같은 요청을 다시 돌려도 결과가 같다). 실행 이름 = 요청 ID 라 중복 실행도 막힌다.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import time

TOKEN_RE = re.compile(r"^member_v\d+_[a-z2-7]{24}$")
ERASED = "erased"

DELETE_TABLES = ["silver.members", "silver.dim_member_scd2", "silver.member_addresses", "silver.events_clean",
                 "gold.dim_member"]  # fmt: skip
PSEUDONYMIZE_TABLES = ["silver.orders", "silver.payments", "silver.payment_events", "silver.coupon_usages",
                       "silver.cs_tickets", "gold.fct_order_items", "gold.fct_payments"]  # fmt: skip
BRONZE = [
    ("bronze.cdc_events", "strpos(coalesce(key_json, ''), '{t}') > 0 OR strpos(coalesce(after_json, ''), '{t}') > 0 "
                          "OR strpos(coalesce(before_json, ''), '{t}') > 0"),
    ("bronze.domain_events", "strpos(coalesce(payload_json, ''), '{t}') > 0"),
    ("bronze.clicks", "member_id = '{t}'"),
]  # fmt: skip


def _check(token: str) -> str:
    if not TOKEN_RE.match(token):
        raise ValueError("회원 토큰 형식이 아니다")  # SQL 에 들어가므로 형식을 엄격히 확인한다
    return token


def retain_sql(request_id: str, token: str) -> list[str]:
    t = _check(token)
    rid = re.sub(r"[^A-Za-z0-9_-]", "", request_id)
    return [
        f"DELETE FROM retention.ecommerce_transactions WHERE request_id = '{rid}'",
        "INSERT INTO retention.ecommerce_transactions "
        f"SELECT '{rid}', '{t}', o.order_id, o.order_no, o.total_amount, o.discount_amount, o.ordered_at, "
        "p.payment_key, p.amount, p.canceled_amount, p.approved_at, current_timestamp "
        "FROM silver.orders o LEFT JOIN silver.payments p ON p.order_no = o.order_no "
        f"WHERE o.member_token = '{t}' AND o.ordered_at >= current_timestamp - INTERVAL '5' YEAR",
        f"DELETE FROM retention.cs_disputes WHERE request_id = '{rid}'",
        "INSERT INTO retention.cs_disputes "
        f"SELECT '{rid}', '{t}', ticket_id, order_id, category, content_masked, created_at, current_timestamp "
        f"FROM silver.cs_tickets WHERE member_token = '{t}' AND created_at >= current_timestamp - INTERVAL '3' YEAR",
    ]


def erase_sql(token: str) -> list[str]:
    t = _check(token)
    out = [f"DELETE FROM {tbl} WHERE member_token = '{t}'" for tbl in DELETE_TABLES]
    for tbl in PSEUDONYMIZE_TABLES:
        extra = ", content_masked = NULL" if tbl == "silver.cs_tickets" else ""
        out.append(f"UPDATE {tbl} SET member_token = '{ERASED}'{extra} WHERE member_token = '{t}'")
    return out


def bronze_sql(token: str) -> list[str]:
    t = _check(token)
    return [f"DELETE FROM {tbl} WHERE {cond.format(t=t)}" for tbl, cond in BRONZE]


def verify_sql(token: str) -> str:
    t = _check(token)
    parts = [f"SELECT '{tbl}' AS t, count(*) AS n FROM {tbl} WHERE member_token = '{t}'"
             for tbl in DELETE_TABLES + PSEUDONYMIZE_TABLES]  # fmt: skip
    parts += [
        f"SELECT '{tbl}' AS t, count(*) AS n FROM {tbl} WHERE {cond.format(t=t)}" for tbl, cond in BRONZE
    ]
    return " UNION ALL ".join(parts)


def tables_touched() -> list[str]:
    return DELETE_TABLES + PSEUDONYMIZE_TABLES + [b for b, _ in BRONZE]


# ---------------------------------------------------------------------------
def _athena(sql: str) -> list[list[str]]:
    import boto3

    a = boto3.client("athena")
    qid = a.start_query_execution(QueryString=sql, WorkGroup=os.environ["ATHENA_WORKGROUP"])[
        "QueryExecutionId"
    ]
    while True:
        ex = a.get_query_execution(QueryExecutionId=qid)["QueryExecution"]
        if ex["Status"]["State"] in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        time.sleep(1.5)
    if ex["Status"]["State"] != "SUCCEEDED":
        raise RuntimeError(
            f"{ex['Status']['State']}: {ex['Status'].get('StateChangeReason', '')} :: {sql[:300]}"
        )
    if ex.get("StatementType") != "DML" or not sql.lstrip().upper().startswith("SELECT"):
        return []
    rows = a.get_query_results(QueryExecutionId=qid)["ResultSet"]["Rows"][1:]
    return [[c.get("VarCharValue") for c in r["Data"]] for r in rows]


def handler(event, _context):
    step = event["step"]
    if step == "trigger":
        return trigger()
    token, rid = event["member_token"], event["request_id"]
    if step == "assess":
        rows = _athena(f"SELECT (SELECT count(*) FROM silver.orders WHERE member_token = '{_check(token)}'), "
                       f"(SELECT count(*) FROM silver.cs_tickets WHERE member_token = '{token}')")  # fmt: skip
        return {
            "orders": int(rows[0][0]),
            "tickets": int(rows[0][1]),
            "needs_retention": int(rows[0][0]) + int(rows[0][1]) > 0,
        }
    if step == "retain":
        for s in retain_sql(rid, token):
            _athena(s)
        return {"retained": True}
    if step == "shred":
        import boto3

        table = boto3.resource("dynamodb").Table(os.environ["MEMBER_KEY_TABLE"])
        table.put_item(Item={"member_token": token, "status": "SHREDDED", "request_id": rid,
                             "shredded_at": dt.datetime.now(dt.UTC).isoformat()})  # fmt: skip
        return {"shredded": True}
    if step == "erase":
        for s in erase_sql(token):
            _athena(s)
        return {"erased_tables": len(DELETE_TABLES) + len(PSEUDONYMIZE_TABLES)}
    if step == "bronze":
        for s in bronze_sql(token):
            _athena(s)
        return {"bronze": True}
    if step == "verify":
        left = {t: int(n) for t, n in _athena(verify_sql(token)) if int(n) > 0}
        if left:
            raise RuntimeError(f"파기 검증 실패: {left}")
        return {"verified": True, "checked_tables": len(tables_touched())}
    raise ValueError(step)


def trigger() -> dict:
    """매시: 새 탈퇴 회원마다 실행 하나. 실행 이름이 요청 ID 라 같은 회원은 두 번 시작되지 않는다."""
    import json

    import boto3

    sfn = boto3.client("stepfunctions")
    rows = _athena("SELECT member_token, CAST(withdrawn_at AS varchar) FROM silver.members "
                   "WHERE status = 'WITHDRAWN' AND withdrawn_at >= current_timestamp - INTERVAL '7' DAY")  # fmt: skip
    started = 0
    for token, withdrawn_at in rows:
        if not TOKEN_RE.match(token or ""):
            continue
        rid = f"erase-{token}"
        try:
            sfn.start_execution(stateMachineArn=os.environ["STATE_MACHINE_ARN"], name=rid,
                                input=json.dumps({"request_id": rid, "member_token": token, "withdrawn_at": withdrawn_at}))  # fmt: skip
            started += 1
        except sfn.exceptions.ExecutionAlreadyExists:
            pass
    return {"candidates": len(rows), "started": started}
