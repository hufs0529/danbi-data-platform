"""Athena 실행 도우미. 워크그룹이 결과 위치·암호화·스캔 상한을 강제하므로 여기서는 지정하지 않는다."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


@dataclass
class QueryResult:
    query_id: str
    rows: list[list[str | None]]
    columns: list[str]
    scanned_bytes: int

    def dicts(self) -> list[dict[str, Any]]:
        return [dict(zip(self.columns, r, strict=False)) for r in self.rows]


class AthenaError(RuntimeError):
    pass


def run(sql: str, workgroup: str, database: str | None = None, client=None, poll_s: float = 1.0,
        timeout_s: float = 900, params: list[str] | None = None) -> QueryResult:  # fmt: skip
    """쿼리를 실행하고 끝날 때까지 기다린다. params 는 Athena 실행 매개변수(? 자리)."""
    if client is None:
        import boto3

        client = boto3.client("athena")
    kwargs: dict[str, Any] = {"QueryString": sql, "WorkGroup": workgroup}
    if database:
        kwargs["QueryExecutionContext"] = {"Database": database}
    if params:
        kwargs["ExecutionParameters"] = params
    qid = client.start_query_execution(**kwargs)["QueryExecutionId"]
    deadline = time.monotonic() + timeout_s
    while True:
        ex = client.get_query_execution(QueryExecutionId=qid)["QueryExecution"]
        state = ex["Status"]["State"]
        if state in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        if time.monotonic() > deadline:
            client.stop_query_execution(QueryExecutionId=qid)
            raise AthenaError(f"timeout {qid}")
        time.sleep(poll_s)
    if state != "SUCCEEDED":
        raise AthenaError(f"{state} {qid}: {ex['Status'].get('StateChangeReason', '')}\n{sql[:500]}")
    scanned = int(ex.get("Statistics", {}).get("DataScannedInBytes", 0))
    if ex.get("StatementType") != "DML":
        return QueryResult(qid, [], [], scanned)
    rows: list[list[str | None]] = []
    columns: list[str] = []
    for i, page in enumerate(client.get_paginator("get_query_results").paginate(QueryExecutionId=qid)):
        rs = page["ResultSet"]
        if not columns:
            columns = [c["Name"] for c in rs["ResultSetMetadata"]["ColumnInfo"]]
        data = rs["Rows"][1:] if i == 0 else rs["Rows"]  # 첫 페이지 첫 행은 헤더
        rows += [[d.get("VarCharValue") for d in r["Data"]] for r in data]
    return QueryResult(qid, rows, columns, scanned)
