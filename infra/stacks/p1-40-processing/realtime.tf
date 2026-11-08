# P1-11 실시간 잡(1분 매출 속보치, 이상결제)과 알림 소비자(Lambda)

variable "realtime_app_key" {
  description = "artifacts 의 Flink 앱 zip(build_flink_app.sh realtime --upload). 비우면 만들지 않는다"
  type        = string
  default     = ""
}

data "aws_ssm_parameter" "msk_arn" {
  name = "${local.ssm_prefix}/p1/streaming/cluster_arn"
}

resource "aws_cloudwatch_log_stream" "realtime" {
  name           = "realtime"
  log_group_name = aws_cloudwatch_log_group.flink.name
}

resource "aws_kinesisanalyticsv2_application" "realtime" {
  count                  = var.realtime_app_key == "" ? 0 : 1
  name                   = "${local.name}-realtime"
  runtime_environment    = "FLINK-1_20"
  service_execution_role = aws_iam_role.flink.arn
  start_application      = var.flink_running

  application_configuration {
    application_code_configuration {
      code_content_type = "ZIPFILE"
      code_content {
        s3_content_location {
          bucket_arn = "arn:aws:s3:::${local.artifacts}"
          file_key   = var.realtime_app_key
        }
      }
    }
    environment_properties {
      property_group {
        property_group_id = "kinesis.analytics.flink.run.options"
        property_map      = { python = "main.py", jarfile = "lib/pyflink-dependencies.jar" }
      }
      property_group {
        property_group_id = "app"
        property_map = {
          region                 = var.region
          bootstrap              = local.in["p1/streaming/bootstrap_brokers_iam"]
          schema_registry_url    = local.in["p1/streaming/schema_registry_url"]
          gold_bucket            = local.in["p1/lakehouse/gold_bucket"]
          hmac_secret_id         = local.in["security/pii_hmac_secret_arn"]
          member_key_table       = aws_dynamodb_table.member_keys.name
          kms_pii_arn            = local.in["security/kms_pii_arn"]
          member_key_cache_ttl_s = "300"
        }
      }
    }
    flink_application_configuration {
      checkpoint_configuration {
        configuration_type            = "CUSTOM"
        checkpointing_enabled         = true
        checkpoint_interval           = 10000 # 이상결제 탐지 p95 ≤ 10초(S-B3)와 Kafka 싱크 지연
        min_pause_between_checkpoints = 2000
      }
      monitoring_configuration {
        configuration_type = "CUSTOM"
        log_level          = "INFO"
        metrics_level      = "OPERATOR"
      }
      parallelism_configuration {
        configuration_type   = "CUSTOM"
        parallelism          = 1
        parallelism_per_kpu  = 1
        auto_scaling_enabled = false
      }
    }
    application_snapshot_configuration {
      snapshots_enabled = true # 상태(5분 카드 이력, 쿨다운)를 업데이트·재시작 너머로 유지(F-09)
    }
    vpc_configuration {
      subnet_ids         = local.app_subnet_ids
      security_group_ids = [local.in["p1/sources/msk_clients_sg_id"]]
    }
  }

  cloudwatch_logging_options {
    log_stream_arn = aws_cloudwatch_log_stream.realtime.arn
  }
}

# ---------------------------------------------------------------------------
# 리스크팀 알림 토픽 + MSK → Lambda(알림 발송, 1분 매출 지표)
# ---------------------------------------------------------------------------
resource "aws_sns_topic" "risk" {
  name              = "${local.name}-risk-alerts"
  kms_master_key_id = local.in["security/kms_logs_arn"]
}

data "archive_file" "alert_lambda" {
  type        = "zip"
  source_file = "${path.module}/../../../p1/streaming/realtime/alert_lambda.py"
  output_path = "${path.module}/.build/alert_lambda.zip"
}

data "aws_iam_policy_document" "lambda_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "alert_lambda" {
  name               = "${local.name}-rt-alerts"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
}

resource "aws_iam_role_policy_attachment" "alert_lambda_vpc" {
  role       = aws_iam_role.alert_lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

data "aws_iam_policy_document" "alert_lambda" {
  statement {
    actions   = ["kafka-cluster:Connect", "kafka-cluster:DescribeCluster", "kafka:DescribeClusterV2", "kafka:GetBootstrapBrokers"]
    resources = ["*"]
  }
  statement {
    actions   = ["kafka-cluster:DescribeTopic", "kafka-cluster:ReadData", "kafka-cluster:DescribeGroup", "kafka-cluster:AlterGroup"]
    resources = ["${local.msk_prefix}:topic/${local.msk_cluster}/*/*", "${local.msk_prefix}:group/${local.msk_cluster}/*/lambda-rt-*"]
  }
  statement {
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.risk.arn]
  }
  statement {
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [local.in["security/kms_logs_arn"]]
  }
  statement {
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["Danbi/Realtime", "Danbi/Pipeline"]
    }
  }
}

resource "aws_iam_role_policy" "alert_lambda" {
  name   = "rt-alerts"
  role   = aws_iam_role.alert_lambda.id
  policy = data.aws_iam_policy_document.alert_lambda.json
}

resource "aws_lambda_function" "alerts" {
  function_name    = "${local.name}-rt-alerts"
  role             = aws_iam_role.alert_lambda.arn
  runtime          = "python3.12"
  architectures    = ["arm64"]
  handler          = "alert_lambda.handler"
  filename         = data.archive_file.alert_lambda.output_path
  source_code_hash = data.archive_file.alert_lambda.output_base64sha256
  timeout          = 60
  memory_size      = 256
  environment {
    variables = { RISK_TOPIC_ARN = aws_sns_topic.risk.arn }
  }
  vpc_config {
    subnet_ids         = local.app_subnet_ids
    security_group_ids = [local.in["p1/sources/msk_clients_sg_id"]]
  }
}

resource "aws_lambda_event_source_mapping" "rt" {
  for_each          = data.aws_ssm_parameter.msk_arn.value == "disabled" ? toset([]) : toset(["alerts.fraud", "rt.sales_1m", "dlq.ingest"])
  event_source_arn  = data.aws_ssm_parameter.msk_arn.value
  function_name     = aws_lambda_function.alerts.arn
  topics            = [each.key]
  starting_position = "LATEST"
  batch_size        = 100
  amazon_managed_kafka_event_source_config {
    consumer_group_id = "lambda-rt-${replace(each.key, ".", "-")}"
  }
}

module "rt_outputs" {
  source = "../../modules/ssm_outputs"
  prefix = "${local.ssm_prefix}/p1/processing"
  values = {
    risk_topic_arn    = aws_sns_topic.risk.arn
    realtime_app_name = "${local.name}-realtime"
    alert_lambda_name = aws_lambda_function.alerts.function_name
  }
}
