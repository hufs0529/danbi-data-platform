-- 대사 결과. 정산일 단위로 덮어쓴다(재실행 동일). 재무·감사가 직접 보는 '증적'이라 WAP 를 거치지 않고 gold 에 바로 쓴다
CREATE TABLE IF NOT EXISTS gold.recon_pg_settlement (
  settle_date     date,
  payment_key     string,
  kind            string,
  internal_amount decimal(18,0),
  pg_amount       decimal(18,0),
  diff            decimal(18,0),
  classification  string  COMMENT 'MATCHED | AMOUNT_MISMATCH | INTERNAL_ONLY | PG_ONLY',
  cause_code      string  COMMENT 'AMOUNT_DIFF | TIMING_NEXT_DAY | PG_PENDING | PG_MISSING | TIMING_PREV_DAY | INTERNAL_STATUS_LAG | UNKNOWN_TXN',
  explained       boolean,
  fee_mismatch    boolean,
  run_id          string,
  created_at      timestamp
)
PARTITIONED BY (month(settle_date))
LOCATION 's3://{gold_bucket}/recon_pg_settlement/'
TBLPROPERTIES ('table_type'='ICEBERG', 'format'='parquet');

-- 대사 1단계(소스 vs 레이크) 결과 이력
CREATE TABLE IF NOT EXISTS ops.recon_source_results (
  check_table   string,
  window_start  timestamp,
  window_end    timestamp,
  source_count  bigint,
  lake_count    bigint,
  source_amount decimal(18,0),
  lake_amount   decimal(18,0),
  ok            boolean,
  checked_at    timestamp
)
LOCATION 's3://{silver_bucket}/_ops/recon_source_results/'
TBLPROPERTIES ('table_type'='ICEBERG', 'format'='parquet');
