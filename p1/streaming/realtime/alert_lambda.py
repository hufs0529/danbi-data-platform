"""MSK 이벤트 소스 매핑 → (1) 이상결제 알림 SNS (2) 1분 매출 CloudWatch 지표.

Lambda 이벤트: {"records": {"<topic>-<partition>": [{"value": base64, "offset": ..}, ...]}}
같은 알림이 재전송돼도(최소 한 번) 한 배치 안에서는 alert_id 로 한 번만 보낸다. 배치 사이 중복은 SNS 구독자(리스크팀)가
alert_id 로 거른다 — 알림은 '놓치는 것'보다 '두 번 받는 것'이 낫다.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os


def dlq_count(event: dict) -> int:
    """DLQ 는 원문(개인정보 포함 가능)이 들어 있다 → 내용은 읽지 않고 건수만 센다(P1-14 DlqMessages 알림)."""
    return sum(
        len(rs) for k, rs in (event.get("records") or {}).items() if k.rsplit("-", 1)[0] == "dlq.ingest"
    )


def parse(event: dict) -> tuple[list[dict], list[dict]]:
    alerts, sales, seen = [], [], set()
    for key, records in (event.get("records") or {}).items():
        topic = key.rsplit("-", 1)[0]
        if topic == "dlq.ingest":
            continue
        for r in records:
            try:
                body = json.loads(base64.b64decode(r["value"]))
            except (ValueError, KeyError, TypeError):
                continue
            if topic == "alerts.fraud" and body.get("alert_id") not in seen:
                seen.add(body.get("alert_id"))
                alerts.append(body)
            elif topic == "rt.sales_1m":
                sales.append(body)
    return alerts, sales


def metric_data(sales: list[dict]) -> list[dict]:
    out = []
    for s in sales:
        ts = dt.datetime.fromisoformat(str(s["window_start"])).replace(tzinfo=dt.UTC)
        for name, field in (("NetSales1m", "net_amount"), ("ApprovedAmount1m", "approved_amount"),
                            ("Payments1m", "payments"), ("NullAmounts1m", "null_amounts")):  # fmt: skip
            out.append(
                {"MetricName": name, "Timestamp": ts, "Value": float(s.get(field) or 0), "Unit": "None"}
            )
    return out


def handler(event, _context):
    import boto3

    alerts, sales = parse(event)
    if alerts:
        sns = boto3.client("sns")
        for a in alerts:
            sns.publish(TopicArn=os.environ["RISK_TOPIC_ARN"], Subject="[이상결제] 동일 카드 다중 배송지",
                        Message=json.dumps(a, ensure_ascii=False, indent=1),
                        MessageAttributes={"alert_id": {"DataType": "String", "StringValue": a["alert_id"]}})  # fmt: skip
    if n := dlq_count(event):
        boto3.client("cloudwatch").put_metric_data(Namespace="Danbi/Pipeline", MetricData=[
            {"MetricName": "DlqMessages", "Dimensions": [{"Name": "Topic", "Value": "dlq.ingest"}], "Value": n}])  # fmt: skip
    data = metric_data(sales)
    if data:
        cw = boto3.client("cloudwatch")
        for i in range(0, len(data), 20):
            cw.put_metric_data(Namespace="Danbi/Realtime", MetricData=data[i : i + 20])
    return {"alerts": len(alerts), "sales_windows": len(sales)}
