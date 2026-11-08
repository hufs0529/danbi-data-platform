-- WAP 게시 기록. 롤백은 previous_snapshot_id 로 한다(S-C2, F-08)
CREATE TABLE IF NOT EXISTS ops.publish_log (
  model                string,
  run_id               string,
  dbt_invocation_id    string,
  audit_snapshot_id    bigint,
  published_snapshot_id bigint,
  previous_snapshot_id bigint,
  row_count            bigint,
  added_columns        string,
  published_at         timestamp
)
LOCATION 's3://{silver_bucket}/_ops/publish_log/'
TBLPROPERTIES ('table_type'='ICEBERG', 'format'='parquet');
