-- VPC 흐름 로그(Parquet, 하이브 파티션)를 Athena에서 조회하기 위한 테이블.
-- 파티션 프로젝션을 써서 MSCK REPAIR 없이 시간 범위로 조회한다.
-- ${bucket}, ${account_id}, ${region} 은 scripts/athena_apply.py 가 치환한다.
CREATE DATABASE IF NOT EXISTS ops;

CREATE EXTERNAL TABLE IF NOT EXISTS ops.vpc_flow_logs (
  version int,
  account_id string,
  interface_id string,
  srcaddr string,
  dstaddr string,
  srcport int,
  dstport int,
  protocol bigint,
  packets bigint,
  bytes bigint,
  start bigint,
  `end` bigint,
  action string,
  log_status string
)
PARTITIONED BY (`aws-account-id` string, `aws-service` string, `aws-region` string,
                `year` string, `month` string, `day` string, `hour` string)
STORED AS PARQUET
LOCATION 's3://${bucket}/vpc-flow-logs/AWSLogs/'
TBLPROPERTIES (
  'projection.enabled' = 'true',
  'projection.aws-account-id.type' = 'enum',
  'projection.aws-account-id.values' = '${account_id}',
  'projection.aws-service.type' = 'enum',
  'projection.aws-service.values' = 'vpcflowlogs',
  'projection.aws-region.type' = 'enum',
  'projection.aws-region.values' = '${region}',
  'projection.year.type' = 'integer', 'projection.year.range' = '2026,2030',
  'projection.month.type' = 'integer', 'projection.month.range' = '1,12', 'projection.month.digits' = '2',
  'projection.day.type' = 'integer', 'projection.day.range' = '1,31', 'projection.day.digits' = '2',
  'projection.hour.type' = 'integer', 'projection.hour.range' = '0,23', 'projection.hour.digits' = '2',
  'storage.location.template' = 's3://${bucket}/vpc-flow-logs/AWSLogs/aws-account-id=${aws-account-id}/aws-service=${aws-service}/aws-region=${aws-region}/year=${year}/month=${month}/day=${day}/hour=${hour}'
);

-- 예: 최근 1시간 동안 NACL/SG에 막힌 트래픽 상위 20 (목적지 포트 기준)
-- SELECT srcaddr, dstaddr, dstport, protocol, count(*) AS flows
-- FROM ops.vpc_flow_logs
-- WHERE action = 'REJECT' AND year = '2026' AND month = '10' AND day = '30' AND hour = '09'
-- GROUP BY 1, 2, 3, 4 ORDER BY flows DESC LIMIT 20;
