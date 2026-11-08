-- 운영 메타 테이블. 위치는 silver 버킷의 _ops/ (LF 등록 범위 안, 분석가 권한 밖)

CREATE TABLE IF NOT EXISTS ops.schema_migrations (
  version    string,
  checksum   string,
  applied_at timestamp
)
LOCATION 's3://{silver_bucket}/_ops/schema_migrations/'
TBLPROPERTIES ('table_type'='ICEBERG', 'format'='parquet');

-- 증분 처리 워터마크: 파이프라인이 Bronze 의 어느 스냅샷까지 처리했는지. 재처리는 이 값을 되돌리는 것
CREATE TABLE IF NOT EXISTS ops.pipeline_watermarks (
  pipeline        string,
  source_table    string,
  target_table    string,
  last_snapshot_id bigint,
  last_ingest_ts  timestamp,
  run_id          string,
  updated_at      timestamp
)
LOCATION 's3://{silver_bucket}/_ops/pipeline_watermarks/'
TBLPROPERTIES ('table_type'='ICEBERG', 'format'='parquet');

-- 계약 밖 컬럼 발견 기록(S-C1). 자동으로 Silver 에 추가하되 분류 전에는 분석가에게 보이지 않는다
CREATE TABLE IF NOT EXISTS ops.schema_drift (
  dataset      string,
  column_name  string,
  observed_type string,
  first_seen_at timestamp,
  run_id       string,
  status       string
)
LOCATION 's3://{silver_bucket}/_ops/schema_drift/'
TBLPROPERTIES ('table_type'='ICEBERG', 'format'='parquet');
