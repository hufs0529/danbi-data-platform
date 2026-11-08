#!/usr/bin/env python3
"""기준 브랜치 대비 바뀐 파일로 plan 해야 할 스택 목록을 계산한다.

- infra/stacks/<s>/** 가 바뀌면 그 스택
- infra/modules/<m>/** 가 바뀌면 그 모듈을 쓰는 모든 스택
- infra/envs/dev/<s>.tfvars 가 바뀌면 그 스택, common.tfvars 면 전부

    python scripts/changed_stacks.py origin/main
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
STACKS = ROOT / "infra" / "stacks"


def all_stacks() -> list[str]:
    return sorted(p.name for p in STACKS.iterdir() if p.is_dir())


def module_users(module: str) -> set[str]:
    users = set()
    pattern = re.compile(rf'source\s*=\s*"\.\./\.\./modules/{re.escape(module)}"')
    for stack in all_stacks():
        for tf in (STACKS / stack).glob("*.tf"):
            if pattern.search(tf.read_text(encoding="utf-8")):
                users.add(stack)
    return users


def stacks_for(paths: list[str]) -> list[str]:
    result: set[str] = set()
    for path in paths:
        parts = pathlib.PurePosixPath(path).parts
        if parts[:2] == ("infra", "stacks") and len(parts) > 2:
            result.add(parts[2])
        elif parts[:2] == ("infra", "modules") and len(parts) > 2:
            result |= module_users(parts[2])
        elif parts[:3] == ("infra", "envs", "dev") and len(parts) > 3:
            name = pathlib.PurePosixPath(parts[3]).stem
            result |= set(all_stacks()) if name == "common" else {name}
    existing = set(all_stacks())
    return sorted(s for s in result if s in existing)


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "origin/main"
    diff = subprocess.run(  # noqa: S603 - 인자는 고정된 git 명령과 브랜치 이름뿐이다
        ["git", "diff", "--name-only", f"{base}...HEAD"],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    print(" ".join(stacks_for(diff.stdout.split())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
