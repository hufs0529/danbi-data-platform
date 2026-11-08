#!/usr/bin/env bash
# Managed Flink(Python) 앱 zip 빌드: 코드 + 계약에서 생성한 PII 정책 + 파이썬 의존성 + 자바 uber jar
#   ./p1/streaming/build_flink_app.sh ingest            # out/flink-ingest-<sha>.zip
#   ./p1/streaming/build_flink_app.sh ingest --upload   # s3://<artifacts>/flink/ 로 업로드, tfvars 에 넣을 키 출력
#   ./p1/streaming/build_flink_app.sh realtime --upload (P1-11)
set -euo pipefail
APP="${1:?ingest|realtime}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SHA="$(git -C "$ROOT" rev-parse --short HEAD)"
BUILD="$(mktemp -d)"
OUT="$ROOT/out"
trap 'rm -rf "$BUILD"' EXIT
mkdir -p "$OUT" "$BUILD/lib" "$BUILD/site-packages"

# 1) 자바 커넥터 uber jar
mvn -q -f "$ROOT/p1/streaming/java/pom.xml" package
cp "$ROOT/p1/streaming/java/target/pyflink-dependencies.jar" "$BUILD/lib/"

# 2) 파이썬 의존성: Managed Flink 1.20 의 Python 3.11 / x86_64 용 바이너리 휠
pip install --quiet --target "$BUILD/site-packages" \
  --platform manylinux2014_x86_64 --python-version 3.11 --implementation cp --only-binary=:all: \
  fastavro==1.12.* cryptography==45.* boto3==1.40.*

# 3) 코드
mkdir -p "$BUILD/p1/streaming" "$BUILD/libs"
cp "$ROOT/p1/__init__.py" "$BUILD/p1/"
cp "$ROOT/p1/streaming/__init__.py" "$BUILD/p1/streaming/"
cp -r "$ROOT/p1/streaming/$APP" "$BUILD/p1/streaming/"
cp -r "$ROOT/p1/streaming/ingest" "$BUILD/p1/streaming/" 2>/dev/null || true   # realtime 도 디코더를 공유
cp -r "$ROOT/libs/danbi_common" "$BUILD/libs/"
cp -r "$ROOT/libs/danbi_common" "$BUILD/site-packages/"
find "$BUILD" -name tests -type d -prune -exec rm -rf {} +

# 4) 계약 → PII 정책(빌드 시점 고정. 계약이 바뀌면 앱을 다시 배포해야 반영된다 = 변경 이력이 배포 이력과 일치)
PYTHONPATH="$ROOT:$ROOT/libs" python3 -c "
import json; from contracts.registry import pii_policy
print(json.dumps(pii_policy(), ensure_ascii=False, indent=1))" > "$BUILD/p1/streaming/ingest/pii_policy.json"

# 5) 진입점
printf 'from p1.streaming.%s.main import main\n\nif __name__ == "__main__":\n    main()\n' "$APP" > "$BUILD/main.py"

ZIP="flink-$APP-$SHA.zip"
(cd "$BUILD" && zip -qr "$OUT/$ZIP" .)
echo "built $OUT/$ZIP"
if [[ "${2:-}" == "--upload" ]]; then
  bucket="$(aws ssm get-parameter --name /danbi/dev/p1/streaming/artifacts_bucket --query Parameter.Value --output text)"
  aws s3 cp "$OUT/$ZIP" "s3://$bucket/flink/$ZIP"
  echo "${APP}_app_key = \"flink/$ZIP\"   # infra/envs/dev/p1-40-processing.tfvars"
fi
