"""스키마·계약 호환성 검사 (CI: python -m contracts.compat --base origin/main). S-C1, NFR-12.

1. Avro(contracts/avro/*.avsc): Schema Registry 와 같은 규칙을 오프라인으로 적용한다.
   BACKWARD = 새 스키마(reader)로 옛 데이터(writer)를 읽을 수 있다 → 소비자를 먼저 배포할 수 있다
   FORWARD  = 옛 스키마로 새 데이터를 읽을 수 있다 → 생산자를 먼저 배포할 수 있다
   FULL     = 둘 다
2. 데이터 계약: 컬럼 삭제·타입 변경·그레인 변경·개인정보 분류 완화를 비호환으로 본다.
   비호환 변경은 v2 데이터셋을 만들어 30일 병행한다(계약의 change_policy).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PROMOTIONS = {"int": {"long", "float", "double"}, "long": {"float", "double"}, "float": {"double"},
              "string": {"bytes"}, "bytes": {"string"}}  # fmt: skip
SUBJECT_LEVEL = {"click_event.avsc": "FULL"}  # schema_registry/admin.py 의 SUBJECT_COMPATIBILITY 와 맞춘다
PERSONAL = {"personal_token", "personal_encrypted", "personal_generalized", "restricted"}


def _name(t) -> str:
    if isinstance(t, dict):
        if t.get("logicalType"):
            return f"{t['type']}:{t['logicalType']}"
        return t.get("name") or t["type"]
    return t


def readable(reader, writer, path: str = "") -> list[str]:
    """reader 스키마로 writer 데이터를 읽을 수 있는가. 문제 목록을 돌려준다."""
    if isinstance(writer, list):  # writer 가 union 이면 모든 갈래를 읽을 수 있어야 한다
        return [e for w in writer for e in readable(reader, w, path)]
    if isinstance(reader, list):
        for r in reader:
            if not readable(r, writer, path):
                return []
        return [f"{path}: union {list(map(_name, reader))} 이 {_name(writer)} 를 읽지 못한다"]
    rt = reader["type"] if isinstance(reader, dict) else reader
    wt = writer["type"] if isinstance(writer, dict) else writer
    if rt == "record" and wt == "record":
        errs = []
        wf = {f["name"]: f for f in writer["fields"]}
        for f in reader["fields"]:
            if f["name"] in wf:
                errs += readable(f["type"], wf[f["name"]]["type"], f"{path}.{f['name']}")
            elif "default" not in f:
                errs.append(f"{path}.{f['name']}: 새 필드에 기본값이 없다(옛 데이터를 읽을 수 없다)")
        return errs
    if rt == "enum" and wt == "enum":
        missing = set(writer["symbols"]) - set(reader["symbols"])
        if missing and "default" not in reader:
            return [f"{path}: enum 심볼 {sorted(missing)} 를 읽지 못한다"]
        return []
    if rt == "array" and wt == "array":
        return readable(reader["items"], writer["items"], f"{path}[]")
    if rt == "map" and wt == "map":
        return readable(reader["values"], writer["values"], f"{path}{{}}")
    if rt == wt:  # 논리 타입(timestamp-millis 등)은 호환성 판정에서 무시된다(Avro 명세)
        return []
    if wt in PROMOTIONS and rt in PROMOTIONS[wt]:
        return []
    return [f"{path}: {_name(writer)} → {_name(reader)} 변환 불가"]


def avro_compat(old: dict, new: dict, level: str) -> list[str]:
    errs = []
    if level in ("BACKWARD", "FULL"):
        errs += [f"BACKWARD {e}" for e in readable(new, old, old.get("name", ""))]
    if level in ("FORWARD", "FULL"):
        errs += [f"FORWARD {e}" for e in readable(old, new, old.get("name", ""))]
    return errs


def contract_compat(old: dict, new: dict) -> list[str]:
    ds = new.get("dataset", old.get("dataset"))
    errs = []
    oc = {c["name"]: c for c in old.get("columns", [])}
    nc = {c["name"]: c for c in new.get("columns", [])}
    for n, c in oc.items():
        if n not in nc:
            errs.append(f"{ds}.{n}: 컬럼 삭제(비호환)")
            continue
        if str(c["type"]).replace(" ", "") != str(nc[n]["type"]).replace(" ", ""):
            errs.append(f"{ds}.{n}: 타입 변경 {c['type']} → {nc[n]['type']}(비호환)")
        if c.get("classification") in PERSONAL and nc[n].get("classification") not in PERSONAL:
            errs.append(
                f"{ds}.{n}: 개인정보 분류 완화 {c['classification']} → {nc[n].get('classification')}(보안 리뷰 필요)"
            )
    if list(old.get("grain", [])) != list(new.get("grain", [])):
        errs.append(f"{ds}: 그레인 변경 {old.get('grain')} → {new.get('grain')}(비호환)")
    return errs


def _git_show(base: str, rel: str) -> str | None:
    r = subprocess.run(["git", "-C", str(ROOT), "show", f"{base}:{rel}"],  # noqa: S603, S607
                       capture_output=True, text=True, check=False)  # fmt: skip
    return r.stdout if r.returncode == 0 else None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--base", default="origin/main")
    args = p.parse_args(argv)
    errs = []
    for f in sorted((ROOT / "contracts" / "avro").glob("*.avsc")):
        old = _git_show(args.base, f"contracts/avro/{f.name}")
        if old:
            errs += [f"{f.name}: {e}" for e in avro_compat(json.loads(old), json.loads(f.read_text()),
                                                            SUBJECT_LEVEL.get(f.name, "BACKWARD"))]  # fmt: skip
    for f in sorted((ROOT / "contracts").glob("p*/*/*.yaml")):
        rel = f.relative_to(ROOT).as_posix()
        old = _git_show(args.base, rel)
        if old:
            errs += contract_compat(yaml.safe_load(old), yaml.safe_load(f.read_text(encoding="utf-8")))
    print("\n".join(errs) or "ok: 호환")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
