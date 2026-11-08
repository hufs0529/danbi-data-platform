"""월간 SLO 달성률·에러 버짓 리포트.

    python -m p1.observability.slo_report --month 2026-10

정책(에러 버짓을 다 쓰면): 해당 데이터 제품의 기능 작업을 멈추고 안정화(원인 제거·자동화·런북 보강)에 집중한다.
달성률 = 좋은 표본 수 / 전체 표본 수 (5분 해상도). 표본이 없는 구간은 '나쁨'으로 센다 — 측정이 멈춘 것도 장애다.
"""

from __future__ import annotations

import argparse
import datetime as dt
import operator
import re

from p1.observability.monitor import load_config

OPS = {"<=": operator.le, "<": operator.lt, ">=": operator.ge, ">": operator.gt, "==": operator.eq}


def is_good(rule: str, value: float) -> bool:
    m = re.match(r"^\s*(<=|>=|==|<|>)\s*([0-9.]+)\s*$", rule)
    if not m:
        raise ValueError(rule)
    return OPS[m.group(1)](value, float(m.group(2)))


def attainment(values: list[float | None], rule: str) -> float:
    if not values:
        return 0.0
    good = sum(1 for v in values if v is not None and is_good(rule, v))
    return good / len(values)


def budget(target: float, actual: float) -> dict:
    allowed = 1 - target
    used = 1 - actual
    remaining = 1.0 if allowed == 0 and used == 0 else (0.0 if allowed == 0 else max(0.0, 1 - used / allowed))
    return {"allowed_bad_ratio": round(allowed, 4), "used_bad_ratio": round(used, 4), "budget_remaining": round(remaining, 3),
            "freeze": remaining <= 0}  # fmt: skip


def render(month: str, rows: list[dict]) -> str:
    lines = [
        f"# SLO 리포트 {month}",
        "",
        "| 데이터 제품 | 목표 | 달성 | 에러 버짓 남음 | 조치 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for r in rows:
        action = "**기능 작업 중지·안정화**" if r["freeze"] else "-"
        lines.append(
            f"| {r['product']} | {r['objective']} | {r['actual']:.2%} | {r['budget_remaining']:.0%} | {action} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--month", required=True)
    args = p.parse_args(argv)
    import boto3

    cw = boto3.client("cloudwatch")
    start = dt.datetime.strptime(args.month + "-01", "%Y-%m-%d").replace(tzinfo=dt.UTC)
    end = (start + dt.timedelta(days=32)).replace(day=1)
    rows = []
    for s in load_config()["slos"]:
        m = s["metric"]
        res = cw.get_metric_data(StartTime=start, EndTime=end, ScanBy="TimestampAscending", MetricDataQueries=[{
            "Id": "m", "ReturnData": True, "MetricStat": {"Stat": "Maximum", "Period": 300, "Metric": {
                "Namespace": m["namespace"], "MetricName": m["name"],
                "Dimensions": [{"Name": k, "Value": v} for k, v in m["dimensions"].items()]}}}])  # fmt: skip
        got = dict(
            zip(
                res["MetricDataResults"][0]["Timestamps"], res["MetricDataResults"][0]["Values"], strict=False
            )
        )
        slots = int((end - start).total_seconds() // 300)
        values = [got.get(start + dt.timedelta(seconds=300 * i)) for i in range(slots)]
        actual = attainment(values, s["good_when"])
        rows.append(
            {
                "product": s["product"],
                "objective": s["objective"],
                "actual": actual,
                **budget(s["target_ratio"], actual),
            }
        )
    print(render(args.month, rows))


if __name__ == "__main__":
    main()
