#!/usr/bin/env bash
# 모든 Terraform 루트(부트스트랩, 모듈, 스택)를 fmt 검사 → init(-backend=false) → validate 하고,
# tests/ 가 있는 모듈은 terraform test 까지 돌린다. 자격증명이 필요 없다.
#   usage: scripts/tf_validate_all.sh [terraform|tofu]
set -uo pipefail
TF=${1:-terraform}
cd "$(dirname "$0")/.."

$TF fmt -check -recursive infra || { echo "fmt 실패: '$TF fmt -recursive infra' 실행"; exit 1; }

rc=0
for d in infra/bootstrap infra/modules/* infra/stacks/*; do
  [ -d "$d" ] || continue
  case "$d" in */p2-*|*/p3-*) continue;; esac   # P1 범위만
  ls "$d"/*.tf >/dev/null 2>&1 || continue
  echo "== $d"
  if ! (cd "$d" && $TF init -backend=false -input=false -no-color >/tmp/tf-init.log 2>&1); then
    tail -n 20 /tmp/tf-init.log; rc=1; continue
  fi
  (cd "$d" && $TF validate -no-color) || rc=1
  if [ -d "$d/tests" ]; then
    (cd "$d" && $TF test -no-color) || rc=1
  fi
done
exit $rc
