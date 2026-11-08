-- 분리 보관소(탈퇴·동의 철회 고객의 법정 보존 기록). 위치는 p1-60-governance 의 retention 버킷(별도 KMS 키)
-- migrate 실행 시 retention_bucket 이 SSM 에 없으면(거버넌스 스택 전) 건너뛴다
CREATE TABLE IF NOT EXISTS retention.ecommerce_transactions (
  request_id      string,
  member_token    string,
  order_id        bigint,
  order_no        string,
  total_amount    decimal(12,0),
  discount_amount decimal(12,0),
  ordered_at      timestamp,
  payment_key     string,
  amount          decimal(12,0),
  canceled_amount decimal(12,0),
  approved_at     timestamp,
  retained_at     timestamp
)
LOCATION 's3://{retention_bucket}/ecommerce_transactions/'
TBLPROPERTIES ('table_type'='ICEBERG', 'format'='parquet');

CREATE TABLE IF NOT EXISTS retention.cs_disputes (
  request_id     string,
  member_token   string,
  ticket_id      bigint,
  order_id       bigint,
  category       string,
  content_masked string,
  created_at     timestamp,
  retained_at    timestamp
)
LOCATION 's3://{retention_bucket}/cs_disputes/'
TBLPROPERTIES ('table_type'='ICEBERG', 'format'='parquet');
