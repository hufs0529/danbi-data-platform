# P1-13 개인정보 보호 운영: 접속기록(해시 체인)·점검, 탈퇴 파기 워크플로(Step Functions), 분리 보관소

data "aws_ssm_parameter" "privacy_in" {
  for_each = toset([
    "security/audit_bucket", "security/kms_logs_arn",
    "p1/lakehouse/workgroup_pipeline", "p1/lakehouse/athena_bucket", "p1/lakehouse/bronze_bucket_arn",
    "p1/processing/emr_app_id", "p1/processing/spark_entry_prefix", "p1/processing/spark_submit_params",
    "p1/processing/emr_log_uri",
  ])
  name = "${local.ssm_prefix}/${each.key}"
}

locals {
  pv         = { for k, p in data.aws_ssm_parameter.privacy_in : k => p.value }
  member_key = "arn:aws:dynamodb:${var.region}:${local.account_id}:table/${local.in["p1/processing/member_key_table"]}"
  erasure_tables = [
    "silver.members", "silver.dim_member_scd2", "silver.member_addresses", "silver.events_clean", "gold.dim_member",
    "silver.orders", "silver.payments", "silver.payment_events", "silver.coupon_usages", "silver.cs_tickets",
    "gold.fct_order_items", "gold.fct_payments", "bronze.cdc_events", "bronze.domain_events", "bronze.clicks",
  ]
}

# ---------------------------------------------------------------------------
# 분리 보관소(동의 철회·탈퇴 고객의 거래 기록, 전자상거래법 시행령 제6조 제2항): 별도 버킷·별도 키·법무 역할만
# ---------------------------------------------------------------------------
module "retention_bucket" {
  source      = "../../modules/secure_bucket"
  name        = "${local.name}-retention-${local.account_id}"
  kms_key_arn = local.in["security/kms_pii_arn"]
  versioning  = true
}

resource "aws_glue_catalog_database" "retention" {
  name         = "retention"
  location_uri = "s3://${module.retention_bucket.id}/"
  description  = "탈퇴·동의 철회 고객의 법정 보존 거래 기록(분리 보관)"
}

resource "aws_lakeformation_resource" "retention" {
  arn                   = module.retention_bucket.arn
  role_arn              = aws_iam_role.lf_access.arn
  hybrid_access_enabled = false
}

resource "aws_iam_role_policy" "lf_access_retention" {
  name = "lf-access-retention"
  role = aws_iam_role.lf_access.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket", "s3:GetBucketLocation"], Resource = [module.retention_bucket.arn, "${module.retention_bucket.arn}/*"] },
      { Effect = "Allow", Action = ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey"], Resource = [local.in["security/kms_pii_arn"]] },
    ]
  })
}

data "aws_iam_policy_document" "legal_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${local.account_id}:root"]
    }
    condition {
      test     = "Bool"
      variable = "aws:MultiFactorAuthPresent"
      values   = ["true"]
    }
  }
}

resource "aws_iam_role" "legal" {
  name                 = "${local.name}-legal"
  description          = "법무·분쟁 담당: 분리 보관 거래 기록 조회 전용"
  assume_role_policy   = data.aws_iam_policy_document.legal_trust.json
  max_session_duration = 3600
}

resource "aws_iam_role_policy" "legal" {
  name = "legal"
  role = aws_iam_role.legal.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["athena:StartQueryExecution", "athena:GetQueryExecution", "athena:GetQueryResults", "athena:GetWorkGroup"], Resource = "arn:aws:athena:${var.region}:${local.account_id}:workgroup/${local.in["p1/lakehouse/workgroup_engineering"]}" },
      { Effect = "Allow", Action = ["glue:GetDatabase", "glue:GetTable", "glue:GetTables", "lakeformation:GetDataAccess"], Resource = "*" },
    ]
  })
}

resource "aws_lakeformation_permissions" "legal_retention" {
  principal   = aws_iam_role.legal.arn
  permissions = ["SELECT", "DESCRIBE"]
  table {
    database_name = aws_glue_catalog_database.retention.name
    wildcard      = true
  }
}

# ---------------------------------------------------------------------------
# 파기 관리대장(DynamoDB): 요청 ID, 대상, 단계별 시각, 검증 결과. 대장 자체는 지우지 않는다(PITR 켬)
# ---------------------------------------------------------------------------
resource "aws_dynamodb_table" "erasure_ledger" {
  name                        = "${local.name}-erasure-ledger"
  billing_mode                = "PAY_PER_REQUEST"
  hash_key                    = "request_id"
  deletion_protection_enabled = true
  attribute {
    name = "request_id"
    type = "S"
  }
  point_in_time_recovery {
    enabled = true
  }
  server_side_encryption {
    enabled     = true
    kms_key_arn = local.in["security/kms_pii_arn"]
  }
}

# ---------------------------------------------------------------------------
# Lambda: 파기 단계 + 접속기록
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "lambda_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

data "archive_file" "erasure" {
  type        = "zip"
  source_file = "${path.module}/../../../p1/privacy/erasure_lambda.py"
  output_path = "${path.module}/.build/erasure_lambda.zip"
}

data "archive_file" "access_log" {
  type        = "zip"
  source_file = "${path.module}/../../../p1/privacy/access_log.py"
  output_path = "${path.module}/.build/access_log.zip"
}

resource "aws_iam_role" "erasure" {
  name               = "${local.name}-erasure"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
}

resource "aws_iam_role_policy_attachment" "erasure_logs" {
  role       = aws_iam_role.erasure.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "erasure" {
  name = "erasure"
  role = aws_iam_role.erasure.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["athena:StartQueryExecution", "athena:GetQueryExecution", "athena:GetQueryResults", "athena:GetWorkGroup"], Resource = "arn:aws:athena:${var.region}:${local.account_id}:workgroup/${local.pv["p1/lakehouse/workgroup_pipeline"]}" },
      { Effect = "Allow", Action = ["glue:GetDatabase", "glue:GetTable", "glue:GetTables", "glue:UpdateTable", "glue:CreateTable", "glue:GetPartitions", "lakeformation:GetDataAccess"], Resource = "*" },
      { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket", "s3:GetBucketLocation"], Resource = [
        local.pv["p1/lakehouse/bronze_bucket_arn"], "${local.pv["p1/lakehouse/bronze_bucket_arn"]}/*",
        local.in["p1/lakehouse/silver_bucket_arn"], "${local.in["p1/lakehouse/silver_bucket_arn"]}/*",
        local.in["p1/lakehouse/gold_bucket_arn"], "${local.in["p1/lakehouse/gold_bucket_arn"]}/*",
        module.retention_bucket.arn, "${module.retention_bucket.arn}/*",
        "arn:aws:s3:::${local.pv["p1/lakehouse/athena_bucket"]}", "arn:aws:s3:::${local.pv["p1/lakehouse/athena_bucket"]}/*",
      ] },
      { Effect = "Allow", Action = ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey"], Resource = [local.in["security/kms_lake_arn"], local.in["security/kms_pii_arn"]] },
      { Effect = "Allow", Action = ["dynamodb:PutItem"], Resource = [local.member_key] },
      { Effect = "Allow", Action = ["states:StartExecution"], Resource = ["arn:aws:states:${var.region}:${local.account_id}:stateMachine:${local.name}-erasure"] },
    ]
  })
}

resource "aws_lambda_function" "erasure" {
  function_name    = "${local.name}-erasure-steps"
  role             = aws_iam_role.erasure.arn
  runtime          = "python3.12"
  architectures    = ["arm64"]
  handler          = "erasure_lambda.handler"
  filename         = data.archive_file.erasure.output_path
  source_code_hash = data.archive_file.erasure.output_base64sha256
  timeout          = 900
  memory_size      = 256
  environment {
    variables = {
      ATHENA_WORKGROUP  = local.pv["p1/lakehouse/workgroup_pipeline"]
      MEMBER_KEY_TABLE  = local.in["p1/processing/member_key_table"]
      STATE_MACHINE_ARN = "arn:aws:states:${var.region}:${local.account_id}:stateMachine:${local.name}-erasure"
    }
  }
}

resource "aws_iam_role" "access_log" {
  name               = "${local.name}-access-log"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
}

resource "aws_iam_role_policy_attachment" "access_log_logs" {
  role       = aws_iam_role.access_log.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "access_log" {
  name = "access-log"
  role = aws_iam_role.access_log.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["s3:GetObject", "s3:ListBucket"], Resource = ["arn:aws:s3:::${local.pv["security/audit_bucket"]}", "arn:aws:s3:::${local.pv["security/audit_bucket"]}/AWSLogs/*", "arn:aws:s3:::${local.pv["security/audit_bucket"]}/access-logs/*"] },
      { Effect = "Allow", Action = ["s3:PutObject"], Resource = ["arn:aws:s3:::${local.pv["security/audit_bucket"]}/access-logs/*", "arn:aws:s3:::${local.pv["security/audit_bucket"]}/inspection/*"] },
      { Effect = "Allow", Action = ["athena:GetQueryExecution", "athena:GetQueryRuntimeStatistics"], Resource = "*" },
      { Effect = "Allow", Action = ["kms:Decrypt", "kms:GenerateDataKey"], Resource = [local.pv["security/kms_logs_arn"]] },
      { Effect = "Allow", Action = ["sns:Publish"], Resource = [local.in["security/alerts_topic_arn"]] },
    ]
  })
}

resource "aws_lambda_function" "access_log" {
  function_name    = "${local.name}-access-log"
  role             = aws_iam_role.access_log.arn
  runtime          = "python3.12"
  architectures    = ["arm64"]
  handler          = "access_log.handler"
  filename         = data.archive_file.access_log.output_path
  source_code_hash = data.archive_file.access_log.output_base64sha256
  timeout          = 900
  memory_size      = 1024
  environment {
    variables = {
      AUDIT_BUCKET     = local.pv["security/audit_bucket"]
      ACCOUNT_ID       = local.account_id
      ALERTS_TOPIC_ARN = local.in["security/alerts_topic_arn"]
    }
  }
}

# ---------------------------------------------------------------------------
# 파기 워크플로(Step Functions). 내부 SLO 72시간(법정 5일) → 실행 제한 시간 72시간, 초과 시 알람
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "sfn_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["states.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "erasure_sfn" {
  name               = "${local.name}-erasure-sfn"
  assume_role_policy = data.aws_iam_policy_document.sfn_trust.json
}

resource "aws_iam_role_policy" "erasure_sfn" {
  name = "erasure-sfn"
  role = aws_iam_role.erasure_sfn.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["lambda:InvokeFunction"], Resource = [aws_lambda_function.erasure.arn] },
      { Effect = "Allow", Action = ["emr-serverless:StartJobRun", "emr-serverless:GetJobRun", "emr-serverless:CancelJobRun"], Resource = "arn:aws:emr-serverless:${var.region}:${local.account_id}:/applications/${local.pv["p1/processing/emr_app_id"]}*" },
      { Effect = "Allow", Action = ["iam:PassRole"], Resource = [local.in["p1/processing/emr_job_role_arn"]] },
      { Effect = "Allow", Action = ["events:PutEvents"], Resource = "arn:aws:events:${var.region}:${local.account_id}:event-bus/default" },
      { Effect = "Allow", Action = ["dynamodb:PutItem", "dynamodb:UpdateItem"], Resource = [aws_dynamodb_table.erasure_ledger.arn] },
      { Effect = "Allow", Action = ["kms:GenerateDataKey", "kms:Decrypt"], Resource = [local.in["security/kms_pii_arn"], local.pv["security/kms_logs_arn"]] },
      { Effect = "Allow", Action = ["sns:Publish"], Resource = [local.in["security/alerts_topic_arn"]] },
      { Effect = "Allow", Action = ["events:PutTargets", "events:PutRule", "events:DescribeRule"], Resource = "arn:aws:events:${var.region}:${local.account_id}:rule/StepFunctions*" },
      { Effect = "Allow", Action = ["logs:CreateLogDelivery", "logs:GetLogDelivery", "logs:UpdateLogDelivery", "logs:DeleteLogDelivery", "logs:ListLogDeliveries", "logs:PutResourcePolicy", "logs:DescribeResourcePolicies", "logs:DescribeLogGroups"], Resource = "*" },
    ]
  })
}

resource "aws_cloudwatch_log_group" "erasure_sfn" {
  name              = "/aws/vendedlogs/states/${local.name}-erasure"
  retention_in_days = 731 # 파기 처리 이력도 감사 대상(2년. CloudWatch 허용값 731)
  kms_key_id        = local.pv["security/kms_logs_arn"]
}

resource "aws_sfn_state_machine" "erasure" {
  name     = "${local.name}-erasure"
  role_arn = aws_iam_role.erasure_sfn.arn
  definition = templatefile("${path.module}/erasure.asl.json", {
    lambda_arn       = aws_lambda_function.erasure.arn
    ledger_table     = aws_dynamodb_table.erasure_ledger.name
    emr_app_id       = local.pv["p1/processing/emr_app_id"]
    emr_role_arn     = local.in["p1/processing/emr_job_role_arn"]
    spark_entry      = "${local.pv["p1/processing/spark_entry_prefix"]}/maintenance.py"
    spark_params     = local.pv["p1/processing/spark_submit_params"]
    emr_log_uri      = local.pv["p1/processing/emr_log_uri"]
    erasure_tables   = join(",", local.erasure_tables)
    alerts_topic_arn = local.in["security/alerts_topic_arn"]
  })
  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.erasure_sfn.arn}:*"
    include_execution_data = false # 실행 입력(회원 토큰)을 로그에 남기지 않는다
    level                  = "ERROR"
  }
}

resource "aws_cloudwatch_metric_alarm" "erasure_slo" {
  alarm_name          = "${local.name}-erasure-slo-breach"
  alarm_description   = "파기 실행이 72시간(내부 SLO)을 넘겼거나 실패했다. 런북: docs/runbooks/erasure.md"
  namespace           = "AWS/States"
  metric_name         = "ExecutionsTimedOut"
  dimensions          = { StateMachineArn = aws_sfn_state_machine.erasure.arn }
  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [local.in["security/alerts_topic_arn"]]
}

resource "aws_cloudwatch_metric_alarm" "erasure_failed" {
  alarm_name          = "${local.name}-erasure-failed"
  alarm_description   = "파기 실행 실패. 대장에서 요청 ID 확인 후 재실행(멱등). 런북: docs/runbooks/erasure.md"
  namespace           = "AWS/States"
  metric_name         = "ExecutionsFailed"
  dimensions          = { StateMachineArn = aws_sfn_state_machine.erasure.arn }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [local.in["security/alerts_topic_arn"]]
}

# ---------------------------------------------------------------------------
# 스케줄(EventBridge Scheduler)
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "scheduler_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "privacy_scheduler" {
  name               = "${local.name}-privacy-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_trust.json
}

resource "aws_iam_role_policy" "privacy_scheduler" {
  name = "invoke"
  role = aws_iam_role.privacy_scheduler.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = ["lambda:InvokeFunction"], Resource = [aws_lambda_function.erasure.arn, aws_lambda_function.access_log.arn] }]
  })
}

locals {
  privacy_schedules = {
    erasure-trigger  = { cron = "cron(5 * * * ? *)", fn = aws_lambda_function.erasure.arn, input = { step = "trigger" } }
    access-log-daily = { cron = "cron(30 1 * * ? *)", fn = aws_lambda_function.access_log.arn, input = { mode = "daily" } }
    access-inspect   = { cron = "cron(0 7 1 * ? *)", fn = aws_lambda_function.access_log.arn, input = { mode = "inspect" } }
    access-verify    = { cron = "cron(0 8 ? * MON *)", fn = aws_lambda_function.access_log.arn, input = { mode = "verify" } }
  }
}

resource "aws_scheduler_schedule" "privacy" {
  for_each                     = local.privacy_schedules
  name                         = "${local.name}-${each.key}"
  schedule_expression          = each.value.cron
  schedule_expression_timezone = "Asia/Seoul"
  flexible_time_window {
    mode = "OFF"
  }
  target {
    arn      = each.value.fn
    role_arn = aws_iam_role.privacy_scheduler.arn
    input    = jsonencode(each.value.input)
  }
}

module "privacy_outputs" {
  source = "../../modules/ssm_outputs"
  prefix = "${local.ssm_prefix}/p1/privacy"
  values = {
    retention_bucket   = module.retention_bucket.id
    erasure_sfn_arn    = aws_sfn_state_machine.erasure.arn
    erasure_ledger     = aws_dynamodb_table.erasure_ledger.name
    erasure_role_arn   = aws_iam_role.erasure.arn
    legal_role_arn     = aws_iam_role.legal.arn
    access_log_fn_name = aws_lambda_function.access_log.function_name
  }
}
