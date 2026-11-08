# P1 오케스트레이션: MWAA(Airflow 3) + OpenLineage 수신(Marquez on ECS)
#
# 역할 분담 (ADR AWS-07)
#   MWAA            : 시간·데이터 도착 기반 배치 DAG (Silver MERGE, Gold, 대사, 유지보수, 백필)
#   Step Functions  : 사람·법이 요구하는 단계형 워크플로(파기 P1-13, P2 재색인) — 단계별 재시도·감사 기록
#   EventBridge     : 이벤트 라우팅(정산 파일 도착 → SQS), 단순 스케줄(RDS 자동 정지)

variable "mwaa_enabled" {
  description = "false 면 환경 삭제(make pause). 시간 과금(mw1.micro 기준 약 $0.29/h, 리전별 상이)"
  type        = bool
  default     = true
}

variable "airflow_version" {
  type    = string
  default = "3.0.6"
}

variable "environment_class" {
  type    = string
  default = "mw1.micro" # 스케줄러 1, 동시 태스크 3, 자동 확장 없음. 대부분의 일은 EMR·ECS 가 하므로 충분
}

variable "webserver_access_mode" {
  description = "PRIVATE_ONLY: 운영 EC2 포트 포워딩으로만 UI 접근. PUBLIC_ONLY 도 IAM 인증이지만 인터넷에 노출된다"
  type        = string
  default     = "PRIVATE_ONLY"
}

variable "marquez_image" {
  type    = string
  default = "marquezproject/marquez:0.51.1"
}

data "aws_ssm_parameter" "in" {
  for_each = toset([
    "network/vpc_id", "network/app_subnet_ids", "network/app_tier_cidr",
    "security/kms_logs_arn", "security/kms_lake_arn", "security/alerts_topic_arn",
    "platform/ecs_cluster_arn", "platform/ecs_cluster_name", "platform/ecs_execution_role_arn", "platform/ecs_log_group",
    "platform/namespace_id", "platform/ops_sg_id",
    "p1/sources/db_clients_sg_id", "p1/sources/msk_clients_sg_id", "p1/sources/pg_endpoint",
    "p1/sources/db_user_secret_prefix",
    "p1/streaming/artifacts_bucket",
    "p1/lakehouse/settlement_queue_url", "p1/lakehouse/settlement_queue_arn", "p1/lakehouse/landing_bucket",
    "p1/lakehouse/recon_task_family", "p1/lakehouse/recon_task_role_arn", "p1/lakehouse/workgroup_pipeline",
    "p1/processing/emr_app_id", "p1/processing/emr_job_role_arn", "p1/processing/spark_entry_prefix",
    "p1/processing/spark_submit_params", "p1/processing/emr_log_uri",
    "p1/processing/dbt_task_family", "p1/processing/dbt_task_role_arn",
  ])
  name = "${local.ssm_prefix}/${each.key}"
}

locals {
  in             = { for k, p in data.aws_ssm_parameter.in : k => p.value }
  app_subnet_ids = split(",", local.in["network/app_subnet_ids"])
  artifacts      = local.in["p1/streaming/artifacts_bucket"]
  marquez_url    = "http://marquez.${var.project}.internal:5000"
}

# ---------------------------------------------------------------------------
# Marquez (OpenLineage 수신·조회 API). 메타데이터 DB 는 결제 RDS 인스턴스의 별도 DB(marquez)
# ---------------------------------------------------------------------------
resource "aws_security_group" "marquez" {
  name        = "${local.name}-sg-marquez"
  description = "Marquez OpenLineage API 5000"
  vpc_id      = local.in["network/vpc_id"]
}

resource "aws_vpc_security_group_ingress_rule" "marquez_from_app" {
  security_group_id = aws_security_group.marquez.id
  cidr_ipv4         = local.in["network/app_tier_cidr"] # 계보를 보내는 주체가 많다(MWAA, dbt, EMR) → 앱 계층 전체
  ip_protocol       = "tcp"
  from_port         = 5000
  to_port           = 5001
  description       = "OpenLineage events and admin from app tier"
}

resource "aws_vpc_security_group_egress_rule" "marquez_all" {
  security_group_id = aws_security_group.marquez.id
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

module "marquez" {
  source             = "../../modules/ecs_service"
  name               = "${local.name}-marquez"
  cluster_arn        = local.in["platform/ecs_cluster_arn"]
  image              = var.marquez_image
  cpu_architecture   = "X86_64" # 공식 이미지가 amd64 기준
  cpu                = 512
  memory             = 1024
  container_port     = 5000
  execution_role_arn = local.in["platform/ecs_execution_role_arn"]
  log_group_name     = local.in["platform/ecs_log_group"]
  environment = {
    MARQUEZ_PORT       = "5000"
    MARQUEZ_ADMIN_PORT = "5001"
    POSTGRES_HOST      = local.in["p1/sources/pg_endpoint"]
    POSTGRES_PORT      = "5432"
    POSTGRES_DB        = "marquez"
  }
  secrets = {
    POSTGRES_USER     = "arn:aws:secretsmanager:${var.region}:${local.account_id}:secret:${local.in["p1/sources/db_user_secret_prefix"]}postgres/marquez:username::"
    POSTGRES_PASSWORD = "arn:aws:secretsmanager:${var.region}:${local.account_id}:secret:${local.in["p1/sources/db_user_secret_prefix"]}postgres/marquez:password::"
  }
  health_check_command           = ["CMD-SHELL", "curl -sf http://localhost:5001/healthcheck || exit 1"]
  create_service                 = true
  desired_count                  = var.mwaa_enabled ? 1 : 0
  subnet_ids                     = local.app_subnet_ids
  security_group_ids             = [aws_security_group.marquez.id, local.in["p1/sources/db_clients_sg_id"]]
  service_discovery_namespace_id = local.in["platform/namespace_id"]
  discovery_name                 = "marquez"
}

# ---------------------------------------------------------------------------
# MWAA
# ---------------------------------------------------------------------------
resource "aws_security_group" "mwaa" {
  name        = "${local.name}-sg-mwaa"
  description = "MWAA workers, scheduler, webserver (self-referencing)"
  vpc_id      = local.in["network/vpc_id"]
}

resource "aws_vpc_security_group_ingress_rule" "mwaa_self" {
  security_group_id            = aws_security_group.mwaa.id
  referenced_security_group_id = aws_security_group.mwaa.id
  ip_protocol                  = "-1"
  description                  = "MWAA internal"
}

resource "aws_vpc_security_group_ingress_rule" "mwaa_ui_from_ops" {
  security_group_id            = aws_security_group.mwaa.id
  referenced_security_group_id = local.in["platform/ops_sg_id"]
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  description                  = "Airflow UI via ops instance port forwarding"
}

resource "aws_vpc_security_group_egress_rule" "mwaa_all" {
  security_group_id = aws_security_group.mwaa.id
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

data "aws_iam_policy_document" "mwaa_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["airflow.amazonaws.com", "airflow-env.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "mwaa" {
  name               = "${local.name}-mwaa"
  assume_role_policy = data.aws_iam_policy_document.mwaa_trust.json
}

data "aws_iam_policy_document" "mwaa" {
  # --- MWAA 자체 운영에 필요한 권한 ---
  statement {
    sid       = "AirflowMetrics"
    actions   = ["airflow:PublishMetrics"]
    resources = ["arn:aws:airflow:${var.region}:${local.account_id}:environment/${local.name}"]
  }
  statement {
    sid       = "DagBucket"
    actions   = ["s3:GetObject*", "s3:GetBucket*", "s3:List*"]
    resources = ["arn:aws:s3:::${local.artifacts}", "arn:aws:s3:::${local.artifacts}/mwaa/*"]
  }
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:CreateLogGroup", "logs:PutLogEvents", "logs:GetLogEvents", "logs:GetLogRecord", "logs:GetLogGroupFields", "logs:GetQueryResults", "logs:DescribeLogGroups"]
    resources = ["arn:aws:logs:${var.region}:${local.account_id}:log-group:airflow-${local.name}-*", "arn:aws:logs:${var.region}:${local.account_id}:log-group:${local.in["platform/ecs_log_group"]}:*"]
  }
  statement {
    sid       = "CeleryQueue"
    actions   = ["sqs:ChangeMessageVisibility", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:GetQueueUrl", "sqs:ReceiveMessage", "sqs:SendMessage"]
    resources = ["arn:aws:sqs:${var.region}:*:airflow-celery-*"]
  }
  statement {
    sid       = "Kms"
    actions   = ["kms:Decrypt", "kms:DescribeKey", "kms:GenerateDataKey*", "kms:Encrypt"]
    resources = [local.in["security/kms_lake_arn"], local.in["security/kms_logs_arn"]]
  }
  statement {
    sid       = "CloudWatchMetrics"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]
  }
  # --- DAG 가 하는 일 ---
  statement {
    sid       = "EmrServerless"
    actions   = ["emr-serverless:StartJobRun", "emr-serverless:GetJobRun", "emr-serverless:CancelJobRun", "emr-serverless:GetApplication", "emr-serverless:StartApplication"]
    resources = ["arn:aws:emr-serverless:${var.region}:${local.account_id}:/applications/${local.in["p1/processing/emr_app_id"]}", "arn:aws:emr-serverless:${var.region}:${local.account_id}:/applications/${local.in["p1/processing/emr_app_id"]}/jobruns/*"]
  }
  statement {
    sid       = "EcsRunTask"
    actions   = ["ecs:RunTask", "ecs:DescribeTasks", "ecs:StopTask", "ecs:DescribeTaskDefinition"]
    resources = ["*"]
    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = [local.in["platform/ecs_cluster_arn"]]
    }
  }
  statement {
    sid     = "PassRoles"
    actions = ["iam:PassRole"]
    resources = [
      local.in["p1/processing/emr_job_role_arn"], local.in["p1/processing/dbt_task_role_arn"],
      local.in["p1/lakehouse/recon_task_role_arn"], local.in["platform/ecs_execution_role_arn"],
    ]
  }
  statement {
    sid       = "SettlementQueue"
    actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:GetQueueUrl", "sqs:ChangeMessageVisibility"]
    resources = [local.in["p1/lakehouse/settlement_queue_arn"]]
  }
  statement {
    sid       = "Config"
    actions   = ["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath"]
    resources = ["arn:aws:ssm:${var.region}:${local.account_id}:parameter${local.ssm_prefix}/airflow/*"]
  }
  statement {
    sid       = "Freshness"
    actions   = ["athena:StartQueryExecution", "athena:GetQueryExecution", "athena:GetQueryResults", "glue:GetTable", "glue:GetDatabase", "s3:GetObject", "s3:PutObject", "s3:ListBucket", "s3:GetBucketLocation"]
    resources = ["*"]
  }
  statement {
    sid       = "Alerts"
    actions   = ["sns:Publish"]
    resources = [local.in["security/alerts_topic_arn"]]
  }
}

resource "aws_iam_role_policy" "mwaa" {
  name   = "mwaa"
  role   = aws_iam_role.mwaa.id
  policy = data.aws_iam_policy_document.mwaa.json
}

# DAG 가 쓰는 설정 한 묶음 = Airflow Variable(SSM 시크릿 백엔드). DAG 는 파싱 시점에 AWS 를 부르지 않는다
resource "aws_ssm_parameter" "airflow_config" {
  name = "${local.ssm_prefix}/airflow/variables/p1_config"
  type = "String"
  value = jsonencode({
    region              = var.region
    ecs_cluster         = local.in["platform/ecs_cluster_name"]
    app_subnets         = local.app_subnet_ids
    db_clients_sg       = local.in["p1/sources/db_clients_sg_id"]
    msk_clients_sg      = local.in["p1/sources/msk_clients_sg_id"]
    emr_app_id          = local.in["p1/processing/emr_app_id"]
    emr_job_role_arn    = local.in["p1/processing/emr_job_role_arn"]
    spark_entry_prefix  = local.in["p1/processing/spark_entry_prefix"]
    spark_submit_params = local.in["p1/processing/spark_submit_params"]
    emr_log_uri         = local.in["p1/processing/emr_log_uri"]
    dbt_task_family     = local.in["p1/processing/dbt_task_family"]
    recon_task_family   = local.in["p1/lakehouse/recon_task_family"]
    landing_bucket      = local.in["p1/lakehouse/landing_bucket"]
    artifacts_bucket    = local.artifacts
    athena_workgroup    = local.in["p1/lakehouse/workgroup_pipeline"]
    alerts_topic_arn    = local.in["security/alerts_topic_arn"]
    ecs_log_group       = local.in["platform/ecs_log_group"]
    marquez_url         = local.marquez_url
  })
}

resource "aws_ssm_parameter" "settlement_queue" {
  name  = "${local.ssm_prefix}/airflow/variables/pg_settlement_queue_url"
  type  = "String"
  value = local.in["p1/lakehouse/settlement_queue_url"]
}

resource "aws_mwaa_environment" "this" {
  count                 = var.mwaa_enabled ? 1 : 0
  name                  = local.name
  airflow_version       = var.airflow_version
  environment_class     = var.environment_class
  execution_role_arn    = aws_iam_role.mwaa.arn
  source_bucket_arn     = "arn:aws:s3:::${local.artifacts}"
  dag_s3_path           = "mwaa/dags"
  requirements_s3_path  = "mwaa/requirements.txt"
  webserver_access_mode = var.webserver_access_mode
  min_workers           = 1
  max_workers           = 1
  kms_key               = local.in["security/kms_lake_arn"]

  airflow_configuration_options = {
    "core.default_timezone"               = "Asia/Seoul"
    "secrets.backend"                     = "airflow.providers.amazon.aws.secrets.systems_manager.SystemsManagerParameterStoreBackend"
    "secrets.backend_kwargs"              = jsonencode({ variables_prefix = "${local.ssm_prefix}/airflow/variables", connections_prefix = "${local.ssm_prefix}/airflow/connections" })
    "openlineage.transport"               = jsonencode({ type = "http", url = local.marquez_url, endpoint = "api/v1/lineage" })
    "openlineage.namespace"               = "${var.project}-${var.env}"
    "core.max_active_runs_per_dag"        = "1"
    "scheduler.min_file_process_interval" = "60"
    # 사용자 설정: DAG 가 파싱 시점에 AWS 호출 없이 읽는다(AIRFLOW__DANBI__PG_SETTLEMENT_QUEUE_URL)
    "danbi.pg_settlement_queue_url" = local.in["p1/lakehouse/settlement_queue_url"]
  }

  network_configuration {
    security_group_ids = [aws_security_group.mwaa.id]
    subnet_ids         = local.app_subnet_ids
  }

  logging_configuration {
    dag_processing_logs {
      enabled   = true
      log_level = "WARNING"
    }
    scheduler_logs {
      enabled   = true
      log_level = "WARNING"
    }
    task_logs {
      enabled   = true
      log_level = "INFO"
    }
    webserver_logs {
      enabled   = false
      log_level = "WARNING"
    }
    worker_logs {
      enabled   = true
      log_level = "WARNING"
    }
  }
}

module "outputs" {
  source = "../../modules/ssm_outputs"
  prefix = "${local.ssm_prefix}/p1/orchestration"
  values = {
    mwaa_name     = local.name
    mwaa_role_arn = aws_iam_role.mwaa.arn
    marquez_url   = local.marquez_url
    mwaa_sg_id    = aws_security_group.mwaa.id
  }
}
