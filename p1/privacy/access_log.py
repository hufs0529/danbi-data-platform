"""개인정보처리시스템 접속기록 (안전성 확보조치 기준 제8조) — Lambda, boto3 만 사용.

    mode=daily   : 어제(KST) CloudTrail 에서 사람의 조회(Athena)·비상 접근·권한 거부를 모아
                   audit 버킷(객체 잠금 2년)에 JSONL 로 쓰고, 일별 해시 체인을 잇는다
    mode=inspect : 지난달 접속기록 점검 리포트(대량 조회, 심야 개인정보 조회, 권한 거부 반복, 비상 접근) + 알림(F-10)
    mode=verify  : 체인 전체를 다시 계산해 위·변조가 없음을 증명한다(S-G3 증빙)

기록 항목(제8조): 계정(누가), 접속 일시(언제), 접속지(어디서: 소스 IP), 처리한 정보주체 정보(무엇: 쿼리·테이블), 수행 업무(어떻게)
"""

from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import json
import os
import re
from collections import defaultdict

KST = dt.timezone(dt.timedelta(hours=9))
HUMAN_ROLE_RE = re.compile(r":assumed-role/(danbi-[a-z]+-(analyst|finance|engineer|breakglass|legal))/(.+)$")
TABLE_RE = re.compile(r"\b(?:from|join)\s+\"?([a-z_]+)\"?\.\"?([a-z0-9_]+)\"?", re.I)
PERSONAL_HINT = re.compile(r"(member_token|_token\b|_enc\b|phone|email|recipient)", re.I)
TOKEN_RE = re.compile(r"\b(member|email|phone|card|device)_v\d+_[a-z2-7]{24}\b")
BULK_ROWS = 10_000
NIGHT_HOURS = range(0, 6)
GENESIS = "0" * 64


# ---------------------------------------------------------------------------
# CloudTrail 레코드 → 접속기록
# ---------------------------------------------------------------------------
def to_access_record(ev: dict) -> dict | None:
    arn = (ev.get("userIdentity") or {}).get("arn", "")
    m = HUMAN_ROLE_RE.search(arn)
    if not m:
        return None  # 파이프라인(서비스 역할)은 접속기록 대상이 아니다(시스템 처리 로그는 별도 보관)
    name, src = ev.get("eventName"), ev.get("eventSource", "")
    rec = {
        "event_id": ev.get("eventID"), "event_time": ev.get("eventTime"), "role": m.group(1),
        "user": (ev.get("userIdentity") or {}).get("sessionContext", {}).get("sourceIdentity") or m.group(3),
        "source_ip": ev.get("sourceIPAddress"), "user_agent": (ev.get("userAgent") or "")[:200],
        "action": f"{src.split('.')[0]}:{name}", "error": ev.get("errorCode"),
    }  # fmt: skip
    if name == "StartQueryExecution" and src == "athena.amazonaws.com":
        q = (ev.get("requestParameters") or {}).get("queryString", "")
        rec["query_id"] = (ev.get("responseElements") or {}).get("queryExecutionId")
        rec["workgroup"] = (ev.get("requestParameters") or {}).get("workGroup")
        rec["tables"] = sorted({f"{a}.{b}".lower() for a, b in TABLE_RE.findall(q)})
        rec["personal_hint"] = bool(PERSONAL_HINT.search(q))
        rec["subject_tokens"] = sorted(
            set(m.group(0) for m in TOKEN_RE.finditer(q))
        )  # S-G3: 정보주체 단위 검색
        rec["query_sha256"] = hashlib.sha256(q.encode()).hexdigest()
        rec["query"] = q[:4000]
        return rec
    if name == "AssumeRole" and "breakglass" in json.dumps(ev.get("requestParameters") or {}):
        rec["action"] = "sts:AssumeRole(breakglass)"
        return rec
    if name == "GetDataAccess" or rec["error"] in ("AccessDenied", "AccessDeniedException"):
        return rec
    return None


def enrich_with_results(records: list[dict], athena) -> None:
    """쿼리 상태·스캔량·반환 행 수(대량 조회 판단). 45일 안의 쿼리만 조회 가능하므로 매일 돈다."""
    for r in records:
        qid = r.get("query_id")
        if not qid:
            continue
        try:
            ex = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]
            r["state"] = ex["Status"]["State"]
            r["state_reason"] = (ex["Status"].get("StateChangeReason") or "")[:300]
            r["scanned_bytes"] = ex.get("Statistics", {}).get("DataScannedInBytes")
            stats = athena.get_query_runtime_statistics(QueryExecutionId=qid)["QueryRuntimeStatistics"]
            r["output_rows"] = stats.get("Rows", {}).get("OutputRows")
        except Exception as e:  # 실패해도 기록은 남긴다(누락보다 불완전이 낫다)
            r["enrich_error"] = type(e).__name__


# ---------------------------------------------------------------------------
# 해시 체인: chain_hash(d) = sha256(chain_hash(d-1) + sha256(file(d)))
# 어느 날의 파일을 고치거나 지우면 그 뒤 모든 체인 값이 달라진다. 체인 파일 자체도 객체 잠금 버킷에 있다
# ---------------------------------------------------------------------------
def chain_entry(day: str, body: bytes, prev_hash: str) -> dict:
    file_hash = hashlib.sha256(body).hexdigest()
    return {"date": day, "file_sha256": file_hash, "prev_chain": prev_hash,
            "chain": hashlib.sha256((prev_hash + file_hash).encode()).hexdigest(), "bytes": len(body)}  # fmt: skip


def verify_chain(entries: list[dict], bodies: dict[str, bytes]) -> list[str]:
    errs, prev = [], GENESIS
    for e in sorted(entries, key=lambda x: x["date"]):
        if e["prev_chain"] != prev:
            errs.append(f"{e['date']}: 앞 체인과 연결이 끊김")
        expect = chain_entry(e["date"], bodies[e["date"]], prev) if e["date"] in bodies else None
        if expect is None:
            errs.append(f"{e['date']}: 접속기록 파일 없음")
        elif expect["chain"] != e["chain"]:
            errs.append(f"{e['date']}: 파일 내용이 체인과 다름(위·변조 의심)")
        prev = e["chain"]
    return errs


# ---------------------------------------------------------------------------
# 점검(F-10, 월 1회 기본 — 2025-10 개정 기준은 내부관리계획에서 주기를 정한다)
# ---------------------------------------------------------------------------
def inspect(records: list[dict]) -> list[dict]:
    findings = []
    denied = defaultdict(int)
    for r in records:
        t = dt.datetime.fromisoformat(r["event_time"].replace("Z", "+00:00")).astimezone(KST)
        if r.get("action") == "sts:AssumeRole(breakglass)":
            findings.append(
                {"rule": "BREAKGLASS_USED", "severity": "high", "user": r["user"], "at": r["event_time"]}
            )
        if (r.get("output_rows") or 0) >= BULK_ROWS and r.get("personal_hint"):
            findings.append({"rule": "BULK_PERSONAL_QUERY", "severity": "high", "user": r["user"],
                             "rows": r["output_rows"], "query_id": r.get("query_id"), "at": r["event_time"]})  # fmt: skip
        if t.hour in NIGHT_HOURS and r.get("personal_hint"):
            findings.append({"rule": "NIGHT_PERSONAL_QUERY", "severity": "medium", "user": r["user"],
                             "query_id": r.get("query_id"), "at": r["event_time"]})  # fmt: skip
        if r.get("error") or (r.get("state") == "FAILED" and "ermission" in (r.get("state_reason") or "")):
            denied[r["user"]] += 1
    for user, n in denied.items():
        if n >= 5:
            findings.append({"rule": "REPEATED_DENIED", "severity": "high", "user": user, "count": n})
    return findings


def render_report(month: str, records: list[dict], findings: list[dict]) -> str:
    users = defaultdict(int)
    for r in records:
        users[r["user"]] += 1
    lines = [f"# 개인정보 접속기록 점검 {month}", "",
             f"- 기록 {len(records)}건, 사용자 {len(users)}명, 발견 {len(findings)}건(high {sum(f['severity'] == 'high' for f in findings)})",
             "- 근거: 개인정보의 안전성 확보조치 기준 제8조(점검 주기는 내부관리계획: 월 1회)", "",
             "| 규칙 | 심각도 | 사용자 | 상세 |", "| --- | --- | --- | --- |"]  # fmt: skip
    for f in findings:
        detail = ", ".join(f"{k}={v}" for k, v in f.items() if k not in ("rule", "severity", "user"))
        lines.append(f"| {f['rule']} | {f['severity']} | {f['user']} | {detail} |")
    lines += [
        "",
        "## 조치 기록",
        "",
        "| 발견 | 확인자 | 사유 확인 결과 | 조치 |",
        "| --- | --- | --- | --- |",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Lambda
# ---------------------------------------------------------------------------
def _cloudtrail_keys(s3, bucket: str, account: str, region: str, day: dt.date) -> list[str]:
    prefix = f"AWSLogs/{account}/CloudTrail/{region}/{day:%Y/%m/%d}/"
    keys = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        keys += [o["Key"] for o in page.get("Contents", [])]
    return keys


def daily(day: dt.date) -> dict:
    import boto3

    s3, athena = boto3.client("s3"), boto3.client("athena")
    bucket, account, region = os.environ["AUDIT_BUCKET"], os.environ["ACCOUNT_ID"], os.environ["AWS_REGION"]
    records = []
    # KST 하루 = UTC 전날 15:00 ~ 당일 15:00 → UTC 이틀치 파일을 읽고 걸러낸다
    for d in (day - dt.timedelta(days=1), day):
        for k in _cloudtrail_keys(s3, bucket, account, region, d):
            body = gzip.decompress(s3.get_object(Bucket=bucket, Key=k)["Body"].read())
            for ev in json.loads(body).get("Records", []):
                t = dt.datetime.fromisoformat(ev["eventTime"].replace("Z", "+00:00")).astimezone(KST).date()
                if t == day and (rec := to_access_record(ev)):
                    records.append(rec)
    enrich_with_results(records, athena)
    body = "\n".join(
        json.dumps(r, ensure_ascii=False, sort_keys=True)
        for r in sorted(records, key=lambda r: r["event_id"] or "")
    ).encode()
    prev_key = f"access-logs/chain/{day - dt.timedelta(days=1)}.json"
    try:
        prev = json.loads(s3.get_object(Bucket=bucket, Key=prev_key)["Body"].read())["chain"]
    except s3.exceptions.NoSuchKey:
        prev = GENESIS
    entry = chain_entry(day.isoformat(), body, prev)
    s3.put_object(
        Bucket=bucket, Key=f"access-logs/dt={day}/access.jsonl", Body=body, ContentType="application/x-ndjson"
    )
    s3.put_object(Bucket=bucket, Key=f"access-logs/chain/{day}.json", Body=json.dumps(entry).encode())
    return {"date": day.isoformat(), "records": len(records), "chain": entry["chain"]}


def handler(event, _context):
    mode = event.get("mode", "daily")
    today = dt.datetime.now(KST).date()
    if mode == "daily":
        return daily(
            dt.date.fromisoformat(event["date"]) if event.get("date") else today - dt.timedelta(days=1)
        )
    import boto3

    s3 = boto3.client("s3")
    bucket = os.environ["AUDIT_BUCKET"]
    if mode == "verify":
        entries, bodies = [], {}
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix="access-logs/chain/"):
            for o in page.get("Contents", []):
                e = json.loads(s3.get_object(Bucket=bucket, Key=o["Key"])["Body"].read())
                entries.append(e)
                bodies[e["date"]] = s3.get_object(
                    Bucket=bucket, Key=f"access-logs/dt={e['date']}/access.jsonl"
                )["Body"].read()
        errs = verify_chain(entries, bodies)
        return {"days": len(entries), "ok": not errs, "errors": errs}
    # inspect: 지난달
    first = today.replace(day=1)
    last_month_end = first - dt.timedelta(days=1)
    month = f"{last_month_end:%Y-%m}"
    records = []
    d = last_month_end.replace(day=1)
    while d <= last_month_end:
        try:
            body = s3.get_object(Bucket=bucket, Key=f"access-logs/dt={d}/access.jsonl")["Body"].read()
            records += [json.loads(x) for x in body.splitlines() if x.strip()]
        except s3.exceptions.NoSuchKey:
            pass
        d += dt.timedelta(days=1)
    findings = inspect(records)
    s3.put_object(Bucket=bucket, Key=f"inspection/{month}/report.md", Body=render_report(month, records, findings).encode(),
                  ContentType="text/markdown; charset=utf-8")  # fmt: skip
    high = [f for f in findings if f["severity"] == "high"]
    if high:
        boto3.client("sns").publish(TopicArn=os.environ["ALERTS_TOPIC_ARN"], Subject=f"[개인정보] 접속기록 점검 {month}: high {len(high)}건",
                                    Message=json.dumps(high, ensure_ascii=False, indent=1)[:200_000])  # fmt: skip
    return {"month": month, "records": len(records), "findings": len(findings)}
