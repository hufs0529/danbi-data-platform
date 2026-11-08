#!/usr/bin/env bash
# EMR Serverless 잡 패키지: 진입 스크립트 + 의존 코드 zip(--py-files)
#   ./p1/batch/build_spark_jobs.sh --upload   → s3://<artifacts>/spark/<sha>/{silver_merge,maintenance,publish_gold}.py, p1-jobs.zip
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SHA="$(git -C "$ROOT" rev-parse --short HEAD)"
OUT="$ROOT/out/spark/$SHA"
BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT
mkdir -p "$OUT" "$BUILD/p1/batch/jobs" "$BUILD/libs"

cp "$ROOT/p1/__init__.py" "$BUILD/p1/"
cp "$ROOT/p1/batch/__init__.py" "$ROOT/p1/batch/transforms.py" "$ROOT/p1/batch/spark-defaults.json" "$BUILD/p1/batch/"
cp "$ROOT/p1/batch/jobs/"*.py "$BUILD/p1/batch/jobs/"
rsync -a --exclude tests --exclude __pycache__ "$ROOT/contracts/" "$BUILD/contracts/"
rsync -a --exclude tests --exclude __pycache__ "$ROOT/libs/danbi_common/" "$BUILD/danbi_common/"
# EMR 이미지에 pyyaml 이 없을 수 있어 순수 파이썬 휠을 함께 넣는다
pip install --quiet --target "$BUILD" --no-deps pyyaml==6.0.2 --no-binary pyyaml 2>/dev/null || pip install --quiet --target "$BUILD" --no-deps pyyaml==6.0.2
(cd "$BUILD" && zip -qr "$OUT/p1-jobs.zip" . -x '*.pyc')

for job in silver_merge maintenance publish_gold; do
  if [[ -f "$ROOT/p1/batch/jobs/$job.py" ]]; then
    printf 'from p1.batch.jobs.%s import main\n\nif __name__ == "__main__":\n    main()\n' "$job" > "$OUT/$job.py"
  fi
done
echo "built $OUT"
if [[ "${1:-}" == "--upload" ]]; then
  bucket="$(aws ssm get-parameter --name /danbi/dev/p1/streaming/artifacts_bucket --query Parameter.Value --output text)"
  aws s3 sync "$OUT" "s3://$bucket/spark/$SHA/"
  echo "spark_jobs_version = \"$SHA\"   # infra/envs/dev/p1-40-processing.tfvars"
fi
