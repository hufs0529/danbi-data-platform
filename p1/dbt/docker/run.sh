#!/usr/bin/env bash
# dbt 를 실행하고 결과물(manifest, run_results)을 S3 에 남긴다. 게시 잡과 계보·감사가 이 파일을 읽는다.
#   run.sh build --select tag:publish
set -uo pipefail
RUN_ID="${DBT_RUN_ID:-manual-$(date -u +%Y%m%dT%H%M%S)}"
DEST="s3://${ARTIFACTS_BUCKET}/dbt/runs/${RUN_ID}"

if [[ -n "${OPENLINEAGE_URL:-}" ]]; then
  # dbt-ol: 실행 결과를 OpenLineage 이벤트로 Marquez 에 보낸다(컬럼 계보 포함)
  export OPENLINEAGE_NAMESPACE="${OPENLINEAGE_NAMESPACE:-danbi-dev}"
  dbt-ol "$@" --target dev
else
  dbt "$@" --target dev
fi
rc=$?
for f in manifest.json run_results.json; do
  [[ -f "target/$f" ]] && aws s3 cp --only-show-errors "target/$f" "$DEST/$f"
done
echo "{\"run_id\": \"${RUN_ID}\", \"exit_code\": ${rc}, \"artifacts\": \"${DEST}\"}"
exit $rc
