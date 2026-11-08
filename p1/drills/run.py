"""훈련 실행 도우미.

python -m p1.drills.run list
python -m p1.drills.run show F-01            # 절차 출력(주입·기대·복구·증빙)
python -m p1.drills.run verify F-01          # 검증 SQL 을 Athena 로 실행해 결과를 남긴다
python -m p1.drills.run postmortem F-01      # 포스트모템 초안(템플릿 + 훈련 정보)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from .catalog import DRILLS, get

ROOT = Path(__file__).resolve().parents[2]


def show(d) -> str:
    lines = [f"# {d.id} {d.title}", "", "## 주입"] + [f"- `{c}`" for c in d.inject]
    lines += ["", f"## 기대 동작\n{d.expect}", "", f"## 수용 기준\n{d.accept}", "", "## 검증"]
    lines += [f"```sql\n{s}\n```" for s in d.verify_sql] or ["- 수동(아래 증빙)"]
    lines += (
        ["", "## 복구"] + [f"- `{c}`" for c in d.restore] + ["", "## 증빙"] + [f"- {e}" for e in d.evidence]
    )
    lines += ["", f"런북: docs/runbooks/{d.runbook}"]
    return "\n".join(lines)


def postmortem(d) -> str:
    tpl = (ROOT / "docs" / "postmortems" / "TEMPLATE.md").read_text(encoding="utf-8")
    return tpl.replace("{{title}}", f"{d.id} {d.title} (훈련)").replace("{{date}}", f"{dt.date.today()}") \
        .replace("{{accept}}", d.accept).replace("{{runbook}}", d.runbook)  # fmt: skip


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["list", "show", "verify", "postmortem"])
    p.add_argument("drill", nargs="?")
    p.add_argument("--workgroup", default="danbi-dev-engineering")
    args = p.parse_args(argv)
    if args.action == "list":
        for d in DRILLS:
            print(f"{d.id}  {d.title}  → {d.runbook}")
        return 0
    d = get(args.drill)
    if args.action == "show":
        print(show(d))
    elif args.action == "postmortem":
        print(postmortem(d))
    else:
        from danbi_common import athena

        out = []
        for sql in d.verify_sql:
            r = athena.run(sql, args.workgroup)
            out.append({"sql": sql, "rows": r.rows[:50], "query_id": r.query_id})
        print(json.dumps({"drill": d.id, "at": dt.datetime.now(dt.UTC).isoformat(), "results": out},
                         ensure_ascii=False, indent=1))  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main())
