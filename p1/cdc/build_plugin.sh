#!/usr/bin/env bash
# MSK Connect 커스텀 플러그인 zip 만들기: Debezium(MySQL·PostgreSQL) + Confluent Avro 컨버터 + Secrets Manager config provider
#   ./p1/cdc/build_plugin.sh            # out/danbi-cdc-plugin-<ver>.zip
#   ./p1/cdc/build_plugin.sh --upload   # s3://<artifacts>/msk-connect/plugins/ 로 업로드
#
# 버전 메모
# - Debezium 3.x 커넥터는 Java 17 이 필요하다. MSK Connect 런타임(Kafka Connect 3.7.x)의 JVM 버전이 문서에 명시돼 있지 않아
#   기본값은 Java 11 호환 2.7 라인이다. 3.x 로 올릴 때는 dev 에서 커넥터를 만들어 UnsupportedClassVersionError 가 없는지 먼저 본다.
set -euo pipefail

DEBEZIUM_VERSION="${DEBEZIUM_VERSION:-2.7.4.Final}"
AVRO_CONVERTER_VERSION="${AVRO_CONVERTER_VERSION:-7.9.2}"
CONFIG_PROVIDER_VERSION="${CONFIG_PROVIDER_VERSION:-0.3.1}"
MAVEN="https://repo1.maven.org/maven2"

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
WORK="$(mktemp -d)"
OUT="$ROOT/out"
NAME="danbi-cdc-plugin-${DEBEZIUM_VERSION%.Final}"
mkdir -p "$OUT" "$WORK/$NAME"
trap 'rm -rf "$WORK"' EXIT

fetch_verified() {  # url dest  — Maven 의 .sha1 로 무결성 확인
  curl -fsSL -o "$2" "$1"
  expected="$(curl -fsSL "$1.sha1" | cut -c1-40)"
  actual="$(sha1sum "$2" | cut -c1-40)"
  [[ "$expected" == "$actual" ]] || { echo "checksum mismatch: $1" >&2; exit 1; }
}

for c in mysql postgres; do
  url="$MAVEN/io/debezium/debezium-connector-$c/$DEBEZIUM_VERSION/debezium-connector-$c-$DEBEZIUM_VERSION-plugin.tar.gz"
  fetch_verified "$url" "$WORK/$c.tar.gz"
  tar -xzf "$WORK/$c.tar.gz" -C "$WORK/$NAME"
done

curl -fsSL -o "$WORK/avro.zip" \
  "https://hub-downloads.confluent.io/api/plugins/confluentinc/kafka-connect-avro-converter/versions/$AVRO_CONVERTER_VERSION/confluentinc-kafka-connect-avro-converter-$AVRO_CONVERTER_VERSION.zip"
unzip -q "$WORK/avro.zip" -d "$WORK/$NAME"

curl -fsSL -o "$WORK/cp.zip" \
  "https://github.com/aws-samples/msk-config-providers/releases/download/r$CONFIG_PROVIDER_VERSION/msk-config-providers-$CONFIG_PROVIDER_VERSION-with-dependencies.zip"
unzip -q "$WORK/cp.zip" -d "$WORK/$NAME/msk-config-providers"

(cd "$WORK" && zip -qr "$OUT/$NAME.zip" "$NAME")
sha256sum "$OUT/$NAME.zip" | tee "$OUT/$NAME.zip.sha256"

if [[ "${1:-}" == "--upload" ]]; then
  bucket="$(aws ssm get-parameter --name /danbi/dev/p1/streaming/artifacts_bucket --query Parameter.Value --output text)"
  aws s3 cp "$OUT/$NAME.zip" "s3://$bucket/msk-connect/plugins/$NAME.zip"
  echo "plugin_key = \"msk-connect/plugins/$NAME.zip\"   # infra/envs/dev/p1-20-streaming.tfvars"
fi
