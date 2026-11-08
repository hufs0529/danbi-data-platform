# P1 처리 계층: Managed Flink(수집·실시간), EMR Serverless(Silver MERGE·유지보수, P1-07)
# 이 파일은 공통 입력과 P1-06 Bronze 수집 잡이다.

variable "flink_running" {
  description = "false 면 앱을 정지(KPU 과금 중단). 재시작 시 마지막 스냅샷에서 이어간다"
  type        = bool
  default     = true
}

variable "ingest_app_key" {
  description = "artifacts 버킷 안의 Flink 앱 zip 키(build_flink_app.sh ingest --upload). 비우면 앱을 만들지 않는다"
  type        = string
  default     = ""
}

variable "ingest_parallelism" {
  type    = number
  default = 1
}

variable "checkpoint_interval_ms" {
  description = "Iceberg 커밋 주기 = Bronze 지연 하한. 짧을수록 작은 파일이 늘어 컴팩션 비용이 커진다"
  type        = number
  default     = 15000
}

data "aws_ssm_parameter" "in" {
  for_each = toset([
    "network/vpc_id", "network/app_subnet_ids",
    "security/kms_lake_arn", "security/kms_pii_arn", "security/kms_logs_arn", "security/pii_hmac_secret_arn",
    "p1/sources/msk_clients_sg_id",
    "p1/streaming/bootstrap_brokers_iam", "p1/streaming/cluster_name", "p1/streaming/artifacts_bucket",
    "p1/streaming/schema_registry_url",
    "p1/lakehouse/bronze_bucket", "p1/lakehouse/silver_bucket", "p1/lakehouse/gold_bucket",
  ])
  name = "${local.ssm_prefix}/${each.key}"
}

locals {
  in             = { for k, p in data.aws_ssm_parameter.in : k => p.value }
  app_subnet_ids = split(",", local.in["network/app_subnet_ids"])
  msk_prefix     = "arn:aws:kafka:${var.region}:${local.account_id}"
  msk_cluster    = local.in["p1/streaming/cluster_name"]
  artifacts      = local.in["p1/streaming/artifacts_bucket"]
}

# ---------------------------------------------------------------------------
# 회원별 데이터 키(crypto-shredding). Flink 가 처음 보는 회원의 키를 만든다
# ---------------------------------------------------------------------------
resource "aws_dynamodb_table" "member_keys" {
  #checkov:skip=CKV_AWS_28:PITR 을 켜면 35일간 폐기한 키를 복구할 수 있어 파기가 완료되지 않는다(ADR P1 crypto-shredding)
  name                        = "${local.name}-member-keys"
  billing_mode                = "PAY_PER_REQUEST"
  hash_key                    = "member_token"
  deletion_protection_enabled = true

  attribute {
    name = "member_token"
    type = "S"
  }

  point_in_time_recovery {
    enabled = false
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = local.in["security/kms_pii_arn"]
  }
}

# ---------------------------------------------------------------------------
# Flink 공통 실행 역할
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "flink_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["kinesisanalytics.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "flink" {
  name               = "${local.name}-flink"
  assume_role_policy = data.aws_iam_policy_document.flink_trust.json
}

data "aws_iam_policy_document" "flink" {
  statement {
    sid       = "MskCluster"
    actions   = ["kafka-cluster:Connect", "kafka-cluster:DescribeCluster", "kafka-cluster:WriteDataIdempotently"]
    resources = ["${local.msk_prefix}:cluster/${local.msk_cluster}/*"]
  }
  statement {
    sid     = "MskRead"
    actions = ["kafka-cluster:DescribeTopic", "kafka-cluster:ReadData"]
    resources = [for t in ["cdc.*", "events.*", "clicks.raw"] :
    "${local.msk_prefix}:topic/${local.msk_cluster}/*/${t}"]
  }
  statement {
    sid     = "MskWrite"
    actions = ["kafka-cluster:DescribeTopic", "kafka-cluster:WriteData"]
    resources = [for t in ["dlq.ingest", "rt.sales_1m", "alerts.fraud"] :
    "${local.msk_prefix}:topic/${local.msk_cluster}/*/${t}"]
  }
  statement {
    sid       = "MskGroups"
    actions   = ["kafka-cluster:AlterGroup", "kafka-cluster:DescribeGroup"]
    resources = ["${local.msk_prefix}:group/${local.msk_cluster}/*/flink-*"]
  }
  statement {
    sid     = "GlueBronzeRt"
    actions = ["glue:GetDatabase", "glue:GetTable", "glue:GetTables", "glue:UpdateTable", "glue:GetPartitions"]
    resources = [
      "arn:aws:glue:${var.region}:${local.account_id}:catalog",
      "arn:aws:glue:${var.region}:${local.account_id}:database/bronze",
      "arn:aws:glue:${var.region}:${local.account_id}:database/rt",
      "arn:aws:glue:${var.region}:${local.account_id}:table/bronze/*",
      "arn:aws:glue:${var.region}:${local.account_id}:table/rt/*",
    ]
  }
  statement {
    sid     = "LakeWrite"
    actions = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket", "s3:GetBucketLocation"]
    resources = [
      "arn:aws:s3:::${local.in["p1/lakehouse/bronze_bucket"]}", "arn:aws:s3:::${local.in["p1/lakehouse/bronze_bucket"]}/*",
      "arn:aws:s3:::${local.in["p1/lakehouse/gold_bucket"]}", "arn:aws:s3:::${local.in["p1/lakehouse/gold_bucket"]}/_rt/*",
    ]
  }
  statement {
    sid       = "AppCode"
    actions   = ["s3:GetObject", "s3:GetObjectVersion"]
    resources = ["arn:aws:s3:::${local.artifacts}/flink/*"]
  }
  statement {
    sid       = "LakeKey"
    actions   = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey"]
    resources = [local.in["security/kms_lake_arn"]]
  }
  statement {
    sid       = "MemberKeys"
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [local.in["security/kms_pii_arn"]]
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "kms:EncryptionContextKeys"
      values   = ["member_token"]
    }
  }
  statement {
    sid       = "MemberKeyTable"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem"]
    resources = [aws_dynamodb_table.member_keys.arn]
  }
  statement {
    sid       = "MemberKeyTableEncryption"
    actions   = ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey"]
    resources = [local.in["security/kms_pii_arn"]]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["dynamodb.${var.region}.amazonaws.com"]
    }
  }
  statement {
    sid       = "HmacSecret"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [local.in["security/pii_hmac_secret_arn"]]
  }
  statement {
    sid       = "Logs"
    actions   = ["logs:PutLogEvents", "logs:DescribeLogGroups", "logs:DescribeLogStreams"]
    resources = ["*"]
  }
  statement {
    sid       = "Metrics"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]
  }
  statement {
    sid = "Vpc"
    actions = [
      "ec2:DescribeVpcs", "ec2:DescribeSubnets", "ec2:DescribeSecurityGroups", "ec2:DescribeDhcpOptions",
      "ec2:CreateNetworkInterface", "ec2:CreateNetworkInterfacePermission", "ec2:DescribeNetworkInterfaces",
      "ec2:DeleteNetworkInterface",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "flink" {
  name   = "flink"
  role   = aws_iam_role.flink.id
  policy = data.aws_iam_policy_document.flink.json
}

resource "aws_cloudwatch_log_group" "flink" {
  name              = "/${var.project}/${var.env}/flink"
  retention_in_days = 30
  kms_key_id        = local.in["security/kms_logs_arn"]
}

resource "aws_cloudwatch_log_stream" "ingest" {
  name           = "bronze-ingest"
  log_group_name = aws_cloudwatch_log_group.flink.name
}

# ---------------------------------------------------------------------------
# P1-06 Bronze 수집 잡
# ---------------------------------------------------------------------------
resource "aws_kinesisanalyticsv2_application" "ingest" {
  count                  = var.ingest_app_key == "" ? 0 : 1
  name                   = "${local.name}-bronze-ingest"
  runtime_environment    = "FLINK-1_20"
  service_execution_role = aws_iam_role.flink.arn
  start_application      = var.flink_running

  application_configuration {
    application_code_configuration {
      code_content_type = "ZIPFILE"
      code_content {
        s3_content_location {
          bucket_arn = "arn:aws:s3:::${local.artifacts}"
          file_key   = var.ingest_app_key
        }
      }
    }

    environment_properties {
      property_group {
        property_group_id = "kinesis.analytics.flink.run.options"
        property_map = {
          python  = "main.py"
          jarfile = "lib/pyflink-dependencies.jar"
        }
      }
      property_group {
        property_group_id = "app"
        property_map = {
          region                 = var.region
          bootstrap              = local.in["p1/streaming/bootstrap_brokers_iam"]
          schema_registry_url    = local.in["p1/streaming/schema_registry_url"]
          bronze_bucket          = local.in["p1/lakehouse/bronze_bucket"]
          hmac_secret_id         = local.in["security/pii_hmac_secret_arn"]
          member_key_table       = aws_dynamodb_table.member_keys.name
          kms_pii_arn            = local.in["security/kms_pii_arn"]
          member_key_cache_ttl_s = "300"
          dlq_topic              = "dlq.ingest"
        }
      }
    }

    flink_application_configuration {
      checkpoint_configuration {
        configuration_type            = "CUSTOM"
        checkpointing_enabled         = true
        checkpoint_interval           = var.checkpoint_interval_ms
        min_pause_between_checkpoints = 5000
      }
      monitoring_configuration {
        configuration_type = "CUSTOM"
        log_level          = "INFO"
        metrics_level      = "TASK"
      }
      parallelism_configuration {
        configuration_type   = "CUSTOM"
        parallelism          = var.ingest_parallelism
        parallelism_per_kpu  = 1
        auto_scaling_enabled = false # 오토스케일 재시작이 체크포인트 지연을 만든다. 부하 테스트 후 결정
      }
    }

    application_snapshot_configuration {
      snapshots_enabled = true # 정지·업데이트 시 스냅샷 → 상태 유지(F-09)
    }

    vpc_configuration {
      subnet_ids         = local.app_subnet_ids
      security_group_ids = [local.in["p1/sources/msk_clients_sg_id"]]
    }
  }

  cloudwatch_logging_options {
    log_stream_arn = aws_cloudwatch_log_stream.ingest.arn
  }
}

module "outputs" {
  source = "../../modules/ssm_outputs"
  prefix = "${local.ssm_prefix}/p1/processing"
  values = {
    member_key_table = aws_dynamodb_table.member_keys.name
    flink_role_arn   = aws_iam_role.flink.arn
    ingest_app_name  = "${local.name}-bronze-ingest"
  }
}
