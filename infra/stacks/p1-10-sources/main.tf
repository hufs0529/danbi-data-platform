# P1 소스 시스템: 주문(MySQL)·결제(PostgreSQL) 운영 DB를 흉내 낸 RDS 2대.
# CDC 를 위해 꼭 필요한 설정만 운영 기본값에서 바꾼다(binlog ROW/GTID, logical replication).

variable "instance_class" {
  type    = string
  default = "db.t4g.micro"
}

variable "backup_retention_days" {
  description = "0 이면 MySQL binlog 가 꺼진다. CDC 를 하려면 1 이상"
  type        = number
  default     = 1
}

variable "pg_max_slot_wal_keep_size_mb" {
  description = "복제 슬롯이 붙잡을 수 있는 WAL 상한. 넘으면 슬롯이 무효화된다(F-03 훈련). 디스크 가득 참보다 낫다"
  type        = number
  default     = 4096
}

variable "auto_stop_enabled" {
  description = "RDS 는 정지 7일 뒤 스스로 켜진다. 매일 새벽 다시 정지시키는 스케줄"
  type        = bool
  default     = true
}

variable "auto_stop_cron" {
  type    = string
  default = "cron(0 2 * * ? *)" # 매일 02:00 KST
}

data "aws_ssm_parameter" "vpc_id" {
  name = "${local.ssm_prefix}/network/vpc_id"
}

data "aws_ssm_parameter" "data_subnet_ids" {
  name = "${local.ssm_prefix}/network/data_subnet_ids"
}

data "aws_ssm_parameter" "ops_sg_id" {
  name = "${local.ssm_prefix}/platform/ops_sg_id"
}

locals {
  vpc_id          = data.aws_ssm_parameter.vpc_id.value
  data_subnet_ids = split(",", data.aws_ssm_parameter.data_subnet_ids.value)
  db_users = {
    "mysql/debezium"     = "CDC 커넥터(복제 권한)"
    "mysql/generator"    = "데이터 생성기(DML)"
    "mysql/recon"        = "대사 잡(읽기 전용)"
    "postgres/debezium"  = "CDC 커넥터(rds_replication)"
    "postgres/generator" = "데이터 생성기(DML)"
    "postgres/recon"     = "대사 잡(읽기 전용)"
  }
}

# ---------------------------------------------------------------------------
# 보안 그룹: 클라이언트 SG 패턴
#   db-clients / msk-clients 는 '신분증' 역할만 한다(인그레스 없음).
#   MSK 는 다음 스택에서 만들지만, 생성기·커넥터가 먼저 신분증을 가져야 하므로 여기서 만든다.
# ---------------------------------------------------------------------------
resource "aws_security_group" "db_clients" {
  name        = "${local.name}-sg-db-clients"
  description = "Clients allowed to reach P1 source databases"
  vpc_id      = local.vpc_id
}

resource "aws_security_group" "msk_clients" {
  name        = "${local.name}-sg-msk-clients"
  description = "Clients allowed to reach MSK and Schema Registry"
  vpc_id      = local.vpc_id
}

resource "aws_vpc_security_group_egress_rule" "clients_all" {
  for_each = {
    db  = aws_security_group.db_clients.id
    msk = aws_security_group.msk_clients.id
  }
  security_group_id = each.value
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
  description       = "Outbound allowed; NACL and route tables bound it"
}

resource "aws_security_group" "mysql" {
  name        = "${local.name}-sg-mysql"
  description = "RDS MySQL (commerce)"
  vpc_id      = local.vpc_id
}

resource "aws_security_group" "postgres" {
  name        = "${local.name}-sg-postgres"
  description = "RDS PostgreSQL (payment)"
  vpc_id      = local.vpc_id
}

resource "aws_vpc_security_group_ingress_rule" "mysql_from_clients" {
  security_group_id            = aws_security_group.mysql.id
  referenced_security_group_id = aws_security_group.db_clients.id
  ip_protocol                  = "tcp"
  from_port                    = 3306
  to_port                      = 3306
  description                  = "db-clients to MySQL"
}

resource "aws_vpc_security_group_ingress_rule" "pg_from_clients" {
  security_group_id            = aws_security_group.postgres.id
  referenced_security_group_id = aws_security_group.db_clients.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  description                  = "db-clients to PostgreSQL"
}

# 사람의 접속은 운영 EC2(SSM 포트 포워딩)만 허용한다
resource "aws_vpc_security_group_ingress_rule" "db_from_ops" {
  for_each = {
    mysql    = { sg = aws_security_group.mysql.id, port = 3306 }
    postgres = { sg = aws_security_group.postgres.id, port = 5432 }
  }
  security_group_id            = each.value.sg
  referenced_security_group_id = data.aws_ssm_parameter.ops_sg_id.value
  ip_protocol                  = "tcp"
  from_port                    = each.value.port
  to_port                      = each.value.port
  description                  = "ops instance port forwarding"
}

resource "aws_db_subnet_group" "data" {
  name       = "${local.name}-data"
  subnet_ids = local.data_subnet_ids
}

# ---------------------------------------------------------------------------
# 파라미터 그룹
# ---------------------------------------------------------------------------
resource "aws_db_parameter_group" "mysql" {
  name   = "${local.name}-mysql80-cdc"
  family = "mysql8.0"

  dynamic "parameter" {
    for_each = {
      binlog_format            = "ROW"     # 행 단위 변경. STATEMENT 면 CDC 불가
      binlog_row_image         = "FULL"    # before/after 전체 컬럼 (SCD2·감사에 필요)
      binlog_row_metadata      = "FULL"    # 컬럼 이름까지 binlog 에 기록
      character_set_server     = "utf8mb4" # 한글·이모지
      collation_server         = "utf8mb4_0900_ai_ci"
      require_secure_transport = "1"
      time_zone                = "UTC" # 운영 DB 는 UTC. 레거시 컬럼 일부는 KST 로 저장된다(계약에 명시)
    }
    content {
      name  = parameter.key
      value = parameter.value
    }
  }

  # GTID 는 재부팅이 필요하다. 장애 조치(F-02) 후 같은 위치에서 이어받으려면 GTID 가 있어야 한다
  parameter {
    name         = "gtid-mode"
    value        = "ON"
    apply_method = "pending-reboot"
  }
  parameter {
    name         = "enforce_gtid_consistency"
    value        = "ON"
    apply_method = "pending-reboot"
  }
}

resource "aws_db_parameter_group" "postgres" {
  name   = "${local.name}-pg16-cdc"
  family = "postgres16"

  parameter {
    name         = "rds.logical_replication"
    value        = "1"
    apply_method = "pending-reboot"
  }
  parameter {
    name         = "max_replication_slots"
    value        = "10"
    apply_method = "pending-reboot"
  }
  parameter {
    name         = "max_wal_senders"
    value        = "10"
    apply_method = "pending-reboot"
  }
  parameter {
    name  = "max_slot_wal_keep_size"
    value = tostring(var.pg_max_slot_wal_keep_size_mb)
  }
  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }
  parameter {
    name  = "timezone"
    value = "UTC"
  }
}

# ---------------------------------------------------------------------------
# RDS 인스턴스. 마스터 암호는 RDS 가 Secrets Manager 에 직접 만들고 회전한다(state 에 남지 않음)
# ---------------------------------------------------------------------------
locals {
  engines = {
    mysql = {
      engine         = "mysql"
      engine_version = "8.0"
      db_name        = "commerce"
      port           = 3306
      params         = aws_db_parameter_group.mysql.name
      sg             = aws_security_group.mysql.id
      logs           = ["error"]
    }
    postgres = {
      engine         = "postgres"
      engine_version = "16"
      db_name        = "payment"
      port           = 5432
      params         = aws_db_parameter_group.postgres.name
      sg             = aws_security_group.postgres.id
      logs           = ["postgresql"]
    }
  }
}

resource "aws_db_instance" "this" {
  for_each = local.engines

  identifier     = "${local.name}-${each.key}"
  engine         = each.value.engine
  engine_version = each.value.engine_version
  instance_class = var.instance_class
  db_name        = each.value.db_name
  port           = each.value.port

  allocated_storage     = 20
  max_allocated_storage = 50
  storage_type          = "gp3"
  storage_encrypted     = true

  username                    = "dbadmin"
  manage_master_user_password = true

  db_subnet_group_name   = aws_db_subnet_group.data.name
  vpc_security_group_ids = [each.value.sg]
  parameter_group_name   = each.value.params
  publicly_accessible    = false
  multi_az               = false # dev. 운영은 true (F-02 장애 조치 훈련은 리부트-장애조치로 대체)

  iam_database_authentication_enabled = true
  ca_cert_identifier                  = "rds-ca-rsa2048-g1"
  backup_retention_period             = var.backup_retention_days
  backup_window                       = "18:00-18:30" # 03:00 KST
  maintenance_window                  = "sun:19:00-sun:19:30"
  auto_minor_version_upgrade          = true
  apply_immediately                   = true
  enabled_cloudwatch_logs_exports     = each.value.logs
  performance_insights_enabled        = false
  deletion_protection                 = false
  skip_final_snapshot                 = true
  copy_tags_to_snapshot               = true
}

# 애플리케이션 계정 비밀: 컨테이너만 만든다. 값(암호)은 p1/sources/migrate.py 가 DB 계정을 만들면서 넣는다.
# 그래서 암호가 Terraform state 에 남지 않는다.
resource "aws_secretsmanager_secret" "db_user" {
  for_each                = local.db_users
  name                    = "${local.name}/p1/${each.key}"
  description             = each.value
  recovery_window_in_days = 0
}

# ---------------------------------------------------------------------------
# 7일 자동 재시작 대응: 매일 새벽 정지 (EventBridge Scheduler 의 AWS SDK 통합 대상)
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "scheduler_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "rds_scheduler" {
  count              = var.auto_stop_enabled ? 1 : 0
  name               = "${local.name}-rds-autostop"
  assume_role_policy = data.aws_iam_policy_document.scheduler_trust.json
}

resource "aws_iam_role_policy" "rds_scheduler" {
  count = var.auto_stop_enabled ? 1 : 0
  name  = "stop-rds"
  role  = aws_iam_role.rds_scheduler[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["rds:StopDBInstance"]
      Resource = [for db in aws_db_instance.this : db.arn]
    }]
  })
}

resource "aws_scheduler_schedule" "rds_autostop" {
  for_each                     = var.auto_stop_enabled ? aws_db_instance.this : {}
  name                         = "${local.name}-${each.key}-autostop"
  schedule_expression          = var.auto_stop_cron
  schedule_expression_timezone = "Asia/Seoul"
  flexible_time_window {
    mode = "OFF"
  }
  target {
    arn      = "arn:aws:scheduler:::aws-sdk:rds:stopDBInstance"
    role_arn = aws_iam_role.rds_scheduler[0].arn
    input    = jsonencode({ DbInstanceIdentifier = each.value.identifier })
    retry_policy {
      maximum_retry_attempts = 0 # 이미 정지 상태면 실패하는 것이 정상
    }
  }
}

module "outputs" {
  source = "../../modules/ssm_outputs"
  prefix = "${local.ssm_prefix}/p1/sources"
  values = {
    mysql_endpoint          = aws_db_instance.this["mysql"].address
    mysql_port              = "3306"
    mysql_instance_id       = aws_db_instance.this["mysql"].identifier
    mysql_master_secret_arn = aws_db_instance.this["mysql"].master_user_secret[0].secret_arn
    pg_endpoint             = aws_db_instance.this["postgres"].address
    pg_port                 = "5432"
    pg_instance_id          = aws_db_instance.this["postgres"].identifier
    pg_master_secret_arn    = aws_db_instance.this["postgres"].master_user_secret[0].secret_arn
    db_user_secret_prefix   = "${local.name}/p1/"
    db_clients_sg_id        = aws_security_group.db_clients.id
    msk_clients_sg_id       = aws_security_group.msk_clients.id
    mysql_sg_id             = aws_security_group.mysql.id
    pg_sg_id                = aws_security_group.postgres.id
  }
}

output "endpoints" {
  value = { for k, db in aws_db_instance.this : k => db.address }
}

# ---------------------------------------------------------------------------
# P1-02 데이터 생성기(ECS 서비스, 기본 0대)와 DB 마이그레이션 태스크
#   생성기는 소스 시스템 흉내이므로 이 스택에 둔다. MSK·랜딩 버킷은 이후 스택에 생기므로
#   권한은 이름 규칙(ARN 패턴)으로 미리 주고, 주소는 실행 시점에 SSM 에서 찾는다.
# ---------------------------------------------------------------------------
variable "generator_image_tag" {
  description = "ECR 태그(불변). git short SHA 를 쓴다"
  type        = string
  default     = "0.1.0"
}

variable "generator_desired_count" {
  description = "0 이면 꺼 둔다. make gen-start 로 1 로 올린다"
  type        = number
  default     = 0
}

variable "generator_args" {
  type    = list(string)
  default = ["p1.generator.cli", "run", "--orders-per-minute", "30", "--clicks-per-second", "20"]
}

data "aws_ssm_parameter" "platform" {
  for_each = toset(["ecs_cluster_arn", "ecs_execution_role_arn", "ecs_log_group", "ecr_registry", "ecr_prefix"])
  name     = "${local.ssm_prefix}/platform/${each.key}"
}

data "aws_ssm_parameter" "app_subnet_ids" {
  name = "${local.ssm_prefix}/network/app_subnet_ids"
}

data "aws_ssm_parameter" "kms_lake_arn" {
  name = "${local.ssm_prefix}/security/kms_lake_arn"
}

locals {
  platform        = { for k, p in data.aws_ssm_parameter.platform : k => p.value }
  generator_image = "${local.platform.ecr_registry}/${local.platform.ecr_prefix}/generator:${var.generator_image_tag}"
  msk_cluster_arn = "arn:aws:kafka:${var.region}:${local.account_id}:cluster/${local.name}-msk/*"
  landing_bucket  = "${local.name}-landing-${local.account_id}"
}

data "aws_iam_policy_document" "generator" {
  statement {
    sid       = "DbSecrets"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.db_user["mysql/generator"].arn, aws_secretsmanager_secret.db_user["postgres/generator"].arn]
  }
  statement {
    sid       = "MskConnect"
    actions   = ["kafka-cluster:Connect", "kafka-cluster:DescribeCluster", "kafka-cluster:WriteDataIdempotently"]
    resources = [local.msk_cluster_arn]
  }
  statement {
    sid       = "MskWriteClicks"
    actions   = ["kafka-cluster:DescribeTopic", "kafka-cluster:WriteData"]
    resources = ["arn:aws:kafka:${var.region}:${local.account_id}:topic/${local.name}-msk/*/clicks.raw"]
  }
  statement {
    sid       = "MskBootstrapParam"
    actions   = ["ssm:GetParameter"]
    resources = ["arn:aws:ssm:${var.region}:${local.account_id}:parameter${local.ssm_prefix}/p1/streaming/*"]
  }
  statement {
    sid       = "SettlementDrop"
    actions   = ["s3:PutObject"]
    resources = ["arn:aws:s3:::${local.landing_bucket}/pg_settlement/*"]
  }
  statement {
    sid       = "LakeKey"
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [data.aws_ssm_parameter.kms_lake_arn.value]
  }
}

module "generator" {
  source             = "../../modules/ecs_service"
  name               = "${local.name}-generator"
  cluster_arn        = local.platform.ecs_cluster_arn
  image              = local.generator_image
  cpu                = 512
  memory             = 1024
  command            = var.generator_args
  execution_role_arn = local.platform.ecs_execution_role_arn
  log_group_name     = local.platform.ecs_log_group
  task_policy_json   = data.aws_iam_policy_document.generator.json
  environment = {
    GEN_MYSQL_SECRET    = aws_secretsmanager_secret.db_user["mysql/generator"].name
    GEN_PG_SECRET       = aws_secretsmanager_secret.db_user["postgres/generator"].name
    MSK_BOOTSTRAP_PARAM = "${local.ssm_prefix}/p1/streaming/bootstrap_brokers_iam"
    SCHEMA_REGISTRY_URL = "http://schema-registry.${var.project}.internal:8081"
    LANDING_BUCKET      = local.landing_bucket
  }
  create_service     = true
  desired_count      = var.generator_desired_count
  capacity_provider  = "FARGATE_SPOT" # 끊겨도 되는 부하 발생기
  subnet_ids         = split(",", data.aws_ssm_parameter.app_subnet_ids.value)
  security_group_ids = [aws_security_group.db_clients.id, aws_security_group.msk_clients.id]
}

data "aws_iam_policy_document" "migrate" {
  statement {
    sid     = "MasterSecrets"
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      aws_db_instance.this["mysql"].master_user_secret[0].secret_arn,
      aws_db_instance.this["postgres"].master_user_secret[0].secret_arn,
    ]
  }
  statement {
    sid       = "WriteAppSecrets"
    actions   = ["secretsmanager:PutSecretValue"]
    resources = [for s in aws_secretsmanager_secret.db_user : s.arn]
  }
  statement {
    sid       = "ReadSourceParams"
    actions   = ["ssm:GetParameter"]
    resources = ["arn:aws:ssm:${var.region}:${local.account_id}:parameter${local.ssm_prefix}/p1/sources/*"]
  }
}

# RunTask 전용(서비스 없음): aws ecs run-task 로 한 번 돌리고 끝난다. 마스터 암호를 읽는 유일한 주체
module "db_migrate" {
  source             = "../../modules/ecs_service"
  name               = "${local.name}-db-migrate"
  cluster_arn        = local.platform.ecs_cluster_arn
  image              = local.generator_image
  cpu                = 256
  memory             = 512
  command            = ["p1.sources.migrate", "--engine", "mysql", "--bootstrap-users"]
  execution_role_arn = local.platform.ecs_execution_role_arn
  log_group_name     = local.platform.ecs_log_group
  task_policy_json   = data.aws_iam_policy_document.migrate.json
  create_service     = false
}

module "generator_outputs" {
  source = "../../modules/ssm_outputs"
  prefix = "${local.ssm_prefix}/p1/generator"
  values = {
    service_name          = module.generator.service_name
    task_definition       = module.generator.task_definition_family
    task_role_name        = module.generator.task_role_name
    migrate_task_family   = module.db_migrate.task_definition_family
    migrate_task_role_arn = module.db_migrate.task_role_arn
  }
}
