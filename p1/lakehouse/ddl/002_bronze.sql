-- Bronze: 도착한 그대로의 불변 로그(append-only). 단, 개인정보는 수집 시점(Flink)에 이미 토큰화·암호화·마스킹되어 있다.
-- 파티션은 수집 일자(ingest_ts). 늦게 도착한 이벤트도 '도착한 날'에 쌓여 증분 처리가 단순해진다.
-- 주의: Iceberg 테이블 버킷에 S3 수명 주기 만료를 걸면 메타데이터가 가리키는 파일이 사라져 테이블이 깨진다.
--       보존 기간 관리는 DELETE + expire_snapshots + remove_orphan_files 로 한다(P1-07 유지보수 잡).

-- CDC 변경 로그. 테이블별 스키마 대신 before/after JSON 으로 둔다:
--   소스 DDL 이 바뀌어도 Bronze 는 깨지지 않고, 해석(형 변환·컬럼 매핑)은 계약을 따르는 Silver 가 맡는다.
CREATE TABLE IF NOT EXISTS bronze.cdc_events (
  source_db       string  COMMENT 'commerce | payment',
  table_name      string,
  op              string  COMMENT 'c=insert u=update d=delete r=snapshot read',
  key_json        string,
  before_json     string,
  after_json      string,
  change_seq      string  COMMENT '정렬 가능한 소스 위치. MySQL binlog file+pos+row, PG LSN. 도착 순서가 아니라 이 값으로 최신을 고른다',
  txn_id          string  COMMENT 'MySQL GTID | PG txId',
  source_ts       timestamp COMMENT '소스 커밋 시각',
  is_snapshot     boolean,
  schema_id       int     COMMENT 'Schema Registry ID',
  kafka_topic     string,
  kafka_partition int,
  kafka_offset    bigint,
  ingest_ts       timestamp
)
PARTITIONED BY (source_db, table_name, day(ingest_ts))
LOCATION 's3://{bronze_bucket}/cdc_events/'
TBLPROPERTIES ('table_type'='ICEBERG', 'format'='parquet', 'write_compression'='zstd');

-- Outbox 도메인 이벤트(events.*)
CREATE TABLE IF NOT EXISTS bronze.domain_events (
  topic           string,
  event_key       string,
  event_id        string,
  event_type      string,
  payload_json    string,
  occurred_at     timestamp,
  schema_id       int,
  kafka_partition int,
  kafka_offset    bigint,
  ingest_ts       timestamp
)
PARTITIONED BY (topic, day(ingest_ts))
LOCATION 's3://{bronze_bucket}/domain_events/'
TBLPROPERTIES ('table_type'='ICEBERG', 'format'='parquet', 'write_compression'='zstd');

-- 클릭스트림(스키마가 고정된 Avro 라 컬럼으로 펼친다). member_id·device_id 는 토큰, search_query 는 마스킹 상태
CREATE TABLE IF NOT EXISTS bronze.clicks (
  event_id        string,
  event_type      string,
  session_id      string,
  member_id       string,
  device_id       string,
  platform        string,
  page            string,
  product_id      bigint,
  search_query    string,
  order_no        string,
  user_agent      string,
  event_time      timestamp,
  sent_time       timestamp,
  schema_id       int,
  kafka_partition int,
  kafka_offset    bigint,
  ingest_ts       timestamp
)
PARTITIONED BY (day(ingest_ts))
LOCATION 's3://{bronze_bucket}/clicks/'
TBLPROPERTIES ('table_type'='ICEBERG', 'format'='parquet', 'write_compression'='zstd');

-- PG 정산 파일 수신 대장(중복·정정·격리 판정 결과)
CREATE TABLE IF NOT EXISTS bronze.pg_settlement_files (
  s3_key          string,
  s3_version_id   string,
  file_name       string,
  settle_date     date,
  seq             int,
  sha256          string,
  size_bytes      bigint,
  header_version  int,
  row_count       int,
  trailer_count   int,
  trailer_amount  decimal(18,0),
  status          string  COMMENT 'ACCEPTED | DUPLICATE | QUARANTINED | SUPERSEDED',
  reason          string,
  received_at     timestamp
)
LOCATION 's3://{bronze_bucket}/pg_settlement_files/'
TBLPROPERTIES ('table_type'='ICEBERG', 'format'='parquet');

CREATE TABLE IF NOT EXISTS bronze.pg_settlement_lines (
  file_sha256     string,
  settle_date     date,
  seq             int,
  line_no         int,
  txn_at          timestamp,
  payment_key     string,
  order_no        string,
  kind            string,
  method          string,
  issuer          string,
  amount          decimal(18,0),
  fee             decimal(18,0),
  vat             decimal(18,0),
  net             decimal(18,0),
  loaded_at       timestamp
)
PARTITIONED BY (month(settle_date))
LOCATION 's3://{bronze_bucket}/pg_settlement_lines/'
TBLPROPERTIES ('table_type'='ICEBERG', 'format'='parquet');
