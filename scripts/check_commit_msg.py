#!/usr/bin/env python3
"""commit-msg 훅: 제목에 [태스크 ID], 본문에 `Task:` 트레일러가 있는지 검사한다."""

from __future__ import annotations

import re
import sys

TASK_ID = r"(C\d{2}|P[12]-\d{2})"
SUBJECT = re.compile(rf"^(feat|fix|infra|docs|test|chore|refactor)\([a-z0-9-]+\): .+ \[{TASK_ID}\]$")
TRAILER = re.compile(rf"^Task: {TASK_ID}$", re.MULTILINE)


def check(message: str) -> list[str]:
    lines = [line for line in message.splitlines() if not line.startswith("#")]
    errors: list[str] = []
    if not lines or not SUBJECT.match(lines[0].strip()):
        errors.append("제목 형식: type(scope): 요약 [P1-04]")
    subject_ids = re.findall(TASK_ID, lines[0]) if lines else []
    trailer = TRAILER.search("\n".join(lines))
    if not trailer:
        errors.append("본문에 'Task: <ID>' 트레일러가 필요하다")
    elif subject_ids and trailer.group(1) != subject_ids[-1]:
        errors.append("제목의 태스크 ID와 Task: 트레일러가 다르다")
    return errors


def main(path: str) -> int:
    with open(path, encoding="utf-8") as f:
        errors = check(f.read())
    for e in errors:
        print(f"commit-msg: {e}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
