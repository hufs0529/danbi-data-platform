#!/usr/bin/env bash
# DAG·requirements 를 MWAA 소스 버킷에 올린다. MWAA 는 DAG 폴더를 주기적으로 동기화한다(requirements 변경은 환경 업데이트 필요)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
bucket="$(aws ssm get-parameter --name /danbi/dev/p1/streaming/artifacts_bucket --query Parameter.Value --output text)"
aws s3 sync --delete --exclude '__pycache__/*' "$ROOT/p1/orchestration/dags/" "s3://$bucket/mwaa/dags/"
aws s3 cp "$ROOT/p1/orchestration/requirements.txt" "s3://$bucket/mwaa/requirements.txt"
echo "synced → s3://$bucket/mwaa/"
