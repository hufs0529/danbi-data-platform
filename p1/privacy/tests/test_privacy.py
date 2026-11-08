import datetime as dt

import pytest

from p1.privacy import access_log as AL
from p1.privacy import erasure_lambda as E
from p1.privacy.kanon import kanon_sql, summarize

TOKEN = "member_v1_" + "a" * 24


def _ct(name="StartQueryExecution", role="danbi-dev-analyst", query="SELECT * FROM gold.fct_order_items",
        when="2026-10-29T03:00:00Z", error=None, src="athena.amazonaws.com", identity="kim@danbi.example"):  # fmt: skip
    return {"eventID": f"e-{name}-{when}", "eventTime": when, "eventName": name, "eventSource": src,
            "sourceIPAddress": "10.20.32.5", "userAgent": "athena-console", "errorCode": error,
            "userIdentity": {"arn": f"arn:aws:sts::1:assumed-role/{role}/sess",
                             "sessionContext": {"sourceIdentity": identity}},
            "requestParameters": {"queryString": query, "workGroup": "danbi-dev-analyst"},
            "responseElements": {"queryExecutionId": "q-1"}}  # fmt: skip


def test_access_record_fields_and_subject_search():
    r = AL.to_access_record(_ct(query=f"SELECT * FROM silver.orders o JOIN silver.payments p ON 1=1 "
                                      f"WHERE o.member_token = '{TOKEN}'"))  # fmt: skip
    assert (
        r["user"] == "kim@danbi.example"
        and r["role"] == "danbi-dev-analyst"
        and r["source_ip"] == "10.20.32.5"
    )
    assert r["tables"] == ["silver.orders", "silver.payments"] and r["personal_hint"]
    assert r["subject_tokens"] == [TOKEN]  # S-G3: 특정 회원을 누가 조회했나


def test_pipeline_roles_are_not_access_logs():
    assert AL.to_access_record(_ct(role="danbi-dev-emr-job")) is None


def test_hash_chain_detects_tampering():
    b1, b2 = b'{"a":1}', b'{"b":2}'
    e1 = AL.chain_entry("2026-10-28", b1, AL.GENESIS)
    e2 = AL.chain_entry("2026-10-29", b2, e1["chain"])
    assert AL.verify_chain([e1, e2], {"2026-10-28": b1, "2026-10-29": b2}) == []
    errs = AL.verify_chain([e1, e2], {"2026-10-28": b'{"a":999}', "2026-10-29": b2})
    assert errs and "위·변조" in errs[0]
    assert AL.verify_chain([e2], {"2026-10-29": b2})  # 앞날 기록을 지우면 연결이 끊긴다


def test_inspection_rules_f10():
    bulk = AL.to_access_record(_ct(role="danbi-dev-engineer", query="SELECT member_token FROM silver.orders",
                                   when="2026-10-29T17:30:00Z"))  # 02:30 KST 심야  # fmt: skip
    bulk["output_rows"] = 50_000
    denied = [
        AL.to_access_record(_ct(error="AccessDenied", when=f"2026-10-29T0{i}:00:00Z")) for i in range(5)
    ]
    bg_event = _ct(name="AssumeRole", src="sts.amazonaws.com", role="danbi-dev-engineer")
    bg_event["requestParameters"] = {"roleArn": "arn:aws:iam::1:role/danbi-dev-breakglass"}
    breakglass = AL.to_access_record(bg_event)
    records = [bulk, *denied, breakglass]
    findings = AL.inspect(records)
    assert {"BULK_PERSONAL_QUERY", "NIGHT_PERSONAL_QUERY", "REPEATED_DENIED", "BREAKGLASS_USED"} <= {
        f["rule"] for f in findings
    }
    report = AL.render_report("2026-10", records, findings)
    assert "제8조" in report and "BULK_PERSONAL_QUERY" in report


def test_erasure_sql_is_idempotent_and_scoped():
    rs = E.retain_sql("erase-x", TOKEN)
    assert rs[0].startswith("DELETE FROM retention.ecommerce_transactions WHERE request_id = 'erase-x'")
    assert "INTERVAL '5' YEAR" in rs[1] and "INTERVAL '3' YEAR" in rs[3]
    es = E.erase_sql(TOKEN)
    assert f"DELETE FROM silver.members WHERE member_token = '{TOKEN}'" in es
    assert any(
        s.startswith("UPDATE silver.orders SET member_token = 'erased'") for s in es
    )  # 거래 사실은 보존
    assert any("content_masked = NULL" in s for s in es)
    assert all(TOKEN in s for s in E.bronze_sql(TOKEN))
    assert E.verify_sql(TOKEN).count("UNION ALL") == len(E.tables_touched()) - 1


def test_erasure_rejects_injection():
    with pytest.raises(ValueError):
        E.erase_sql("member_v1_x' OR '1'='1")


def test_kanon():
    assert "HAVING count(*) < 5" in kanon_sql()
    s = summarize([("1990", "강남구", "VIP", "2"), ("1980", "중구", "GOLD", "1")], total=200)
    assert s["members_in_risky_groups"] == 3 and s["recommendation"].startswith("일반화")


def test_kst_day_boundary():
    # 15:30 UTC 는 다음 날 00:30 KST
    t = dt.datetime(2026, 10, 29, 15, 30, tzinfo=dt.UTC).astimezone(AL.KST)
    assert t.date() == dt.date(2026, 10, 30)
