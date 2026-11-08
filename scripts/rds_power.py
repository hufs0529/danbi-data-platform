"""프로젝트 RDS 인스턴스 정지·시작 (make pause / make resume 에서 호출).

- 태그(project, env)로 대상을 찾는다. 이름 규칙이 바뀌어도 안전하다.
- RDS 는 정지 7일 뒤 자동으로 다시 켜진다. 그래서 p1-10-sources 에 매일 정지 스케줄이 있다.
- 이미 원하는 상태면 건너뛴다(멱등). 중간 상태(starting/stopping)면 기다린다.
"""

from __future__ import annotations

import argparse
import sys
import time

TRANSITIONAL = {
    "starting",
    "stopping",
    "backing-up",
    "modifying",
    "rebooting",
    "configuring-enhanced-monitoring",
}


def find_instances(rds, project: str, env: str) -> list[dict]:
    found = []
    for page in rds.get_paginator("describe_db_instances").paginate():
        for db in page["DBInstances"]:
            tags = {t["Key"]: t["Value"] for t in db.get("TagList", [])}
            if tags.get("project") == project and tags.get("env") == env:
                found.append(db)
    return found


def _status(rds, ident: str) -> str:
    return rds.describe_db_instances(DBInstanceIdentifier=ident)["DBInstances"][0]["DBInstanceStatus"]


def wait_stable(rds, ident: str, timeout_s: int = 1200, poll_s: int = 15) -> str:
    deadline = time.monotonic() + timeout_s
    status = _status(rds, ident)
    while status in TRANSITIONAL and time.monotonic() < deadline:
        time.sleep(poll_s)
        status = _status(rds, ident)
    return status


def apply(
    rds, action: str, project: str, env: str, wait: bool = True, poll_s: int = 15
) -> list[tuple[str, str]]:
    results = []
    for db in find_instances(rds, project, env):
        ident = db["DBInstanceIdentifier"]
        status = db["DBInstanceStatus"]
        if status in TRANSITIONAL:
            status = wait_stable(rds, ident, poll_s=poll_s)
        if action == "stop":
            if status == "available":
                rds.stop_db_instance(DBInstanceIdentifier=ident)
                results.append((ident, "stopping"))
            else:
                results.append((ident, f"skip({status})"))
        else:
            if status == "stopped":
                rds.start_db_instance(DBInstanceIdentifier=ident)
                results.append((ident, "starting"))
                if wait:
                    rds.get_waiter("db_instance_available").wait(DBInstanceIdentifier=ident)
                    results[-1] = (ident, "available")
            else:
                results.append((ident, f"skip({status})"))
    return results


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["stop", "start", "status"])
    p.add_argument("--project", default="danbi")
    p.add_argument("--env", default="dev")
    p.add_argument("--no-wait", action="store_true")
    args = p.parse_args(argv)

    import boto3

    rds = boto3.client("rds")
    if args.action == "status":
        for db in find_instances(rds, args.project, args.env):
            print(f"{db['DBInstanceIdentifier']}: {db['DBInstanceStatus']}")
        return 0
    for ident, result in apply(rds, args.action, args.project, args.env, wait=not args.no_wait):
        print(f"{ident}: {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
