"""접근 요청 YAML 검사(CI). 승인자 2명, 최대 30일, 허용된 범위만."""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import yaml

DIR = Path(__file__).resolve().parents[2] / "governance" / "access"
MAX_DAYS = 30
ALLOWED_CLASSES = {
    "public",
    "internal",
    "personal_token",
    "personal_generalized",
}  # 암호문·restricted 는 break-glass 로만
REQUIRED = {"id", "principal_arn", "requester", "reason", "approved_by", "ticket", "scope", "expires_at"}


def check(req: dict, name: str, today: dt.datetime) -> list[str]:
    errs = [f"{name}: 필드 누락 {sorted(REQUIRED - set(req))}"] if REQUIRED - set(req) else []
    if errs:
        return errs
    if req["id"] != name:
        errs.append(f"{name}: id 와 파일명이 다르다")
    if len(set(req["approved_by"])) < 2 or req["requester"] in req["approved_by"]:
        errs.append(f"{name}: 요청자가 아닌 승인자 2명 필요")
    exp = dt.datetime.fromisoformat(str(req["expires_at"]).replace("Z", "+00:00"))
    created = dt.datetime.strptime(name[:10], "%Y-%m-%d").replace(tzinfo=dt.UTC)
    if (exp - created).days > MAX_DAYS:
        errs.append(f"{name}: 기간 {(exp - created).days}일 > {MAX_DAYS}일")
    if bad := set(req["scope"].get("classification", [])) - ALLOWED_CLASSES:
        errs.append(f"{name}: 요청할 수 없는 분류 {sorted(bad)}(비상 접근 절차 사용)")
    if not set(req["scope"].get("layer", [])) <= {"gold", "silver"}:
        errs.append(f"{name}: layer 는 gold·silver 만")
    return errs


def main() -> int:
    now = dt.datetime.now(dt.UTC)
    errs = []
    for p in sorted(DIR.glob("*.yaml")):
        errs += check(yaml.safe_load(p.read_text(encoding="utf-8")), p.stem, now)
    print("\n".join(errs) or "ok")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
