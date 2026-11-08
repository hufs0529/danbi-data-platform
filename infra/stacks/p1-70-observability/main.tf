# P1 관측: 데이터 신선도·게시 기한·커넥터 상태 지표(Lambda), SLO·증상 알람(slos.yaml 단일 원본), 대시보드
#
# 관측 3층
#   인프라(브로커·워커 CPU 등)  : AWS 기본 지표 → 대시보드만(알림 없음)
#   파이프라인(지연·체크포인트) : 컨슈머 시간 지연, Flink 체크포인트, 커넥터 상태, DLQ → 증상 알림
#   데이터(신선도·대사·품질)    : 이 스택의 Lambda 지표 + Danbi/Recon → SLO 알림

data "aws_ssm_parameter" "in" {
  for_each = toset([
    "security/alerts_topic_arn", "security/kms_lake_arn",
    "p1/lakehouse/workgroup_pipeline", "p1/lakehouse/athena_bucket",
    "p1/lakehouse/bronze_bucket_arn", "p1/lakehouse/silver_bucket_arn", "p1/lakehouse/gold_bucket_arn",
    "p1/processing/ingest_app_name", "p1/processing/realtime_app_name",
  ])
  name = "${local.ssm_prefix}/${each.key}"
}

locals {
  in      = { for k, p in data.aws_ssm_parameter.in : k => p.value }
  cfg     = yamldecode(file("${path.module}/../../../p1/observability/slos.yaml"))
  runbook = "https://github.com/hufs0529/danbi-data-platform/blob/main/docs/runbooks"
  alarms = merge(
    { for s in local.cfg.slos : s.id => merge(s, { statistic = "Maximum", kind = "SLO" }) },
    { for a in local.cfg.alerts : a.id => merge(a, { statistic = lookup(a, "statistic", "Maximum"), kind = "증상", objective = lookup(a, "note", "") }) },
  )
}

# ---------------------------------------------------------------------------
# 관측 Lambda
# ---------------------------------------------------------------------------
data "archive_file" "monitor" {
  type        = "zip"
  output_path = "${path.module}/.build/monitor.zip"
  source {
    content  = file("${path.module}/../../../p1/observability/monitor.py")
    filename = "monitor.py"
  }
  source {
    content  = jsonencode(local.cfg) # Lambda 에 PyYAML 이 없으므로 JSON 으로 넣는다
    filename = "slos.json"
  }
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

resource "aws_iam_role" "monitor" {
  name               = "${local.name}-data-monitor"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
}

resource "aws_iam_role_policy_attachment" "monitor_logs" {
  role       = aws_iam_role.monitor.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "monitor" {
  name = "monitor"
  role = aws_iam_role.monitor.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["glue:GetTable"], Resource = "*" },
      # 메타데이터 JSON 만 읽는다(데이터 파일 접근 없음)
      { Effect = "Allow", Action = ["s3:GetObject"], Resource = [for b in ["bronze", "silver", "gold"] : "${local.in["p1/lakehouse/${b}_bucket_arn"]}/*/metadata/*"] },
      { Effect = "Allow", Action = ["kms:Decrypt"], Resource = [local.in["security/kms_lake_arn"]] },
      { Effect = "Allow", Action = ["kafkaconnect:ListConnectors"], Resource = "*" },
      { Effect = "Allow", Action = ["cloudwatch:PutMetricData"], Resource = "*", Condition = { StringLike = { "cloudwatch:namespace" = ["Danbi/*"] } } },
      { Effect = "Allow", Action = ["athena:StartQueryExecution", "athena:GetQueryExecution", "athena:GetQueryResults"], Resource = "arn:aws:athena:${var.region}:${local.account_id}:workgroup/${local.in["p1/lakehouse/workgroup_pipeline"]}" },
      { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject", "s3:ListBucket", "s3:GetBucketLocation"], Resource = ["arn:aws:s3:::${local.in["p1/lakehouse/athena_bucket"]}", "arn:aws:s3:::${local.in["p1/lakehouse/athena_bucket"]}/*"] },
      { Effect = "Allow", Action = ["lakeformation:GetDataAccess"], Resource = "*" },
    ]
  })
}

resource "aws_lambda_function" "monitor" {
  function_name    = "${local.name}-data-monitor"
  role             = aws_iam_role.monitor.arn
  runtime          = "python3.12"
  architectures    = ["arm64"]
  handler          = "monitor.handler"
  filename         = data.archive_file.monitor.output_path
  source_code_hash = data.archive_file.monitor.output_base64sha256
  timeout          = 300
  memory_size      = 256
  environment {
    variables = {
      ATHENA_WORKGROUP = local.in["p1/lakehouse/workgroup_pipeline"]
      CONNECTOR_PREFIX = "${local.name}-"
    }
  }
}

data "aws_iam_policy_document" "scheduler_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${local.name}-monitor-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_trust.json
}

resource "aws_iam_role_policy" "scheduler" {
  name = "invoke"
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = ["lambda:InvokeFunction"], Resource = [aws_lambda_function.monitor.arn] }]
  })
}

resource "aws_scheduler_schedule" "monitor" {
  for_each = {
    freshness = { cron = "rate(5 minutes)", input = { mode = "freshness" } }
    health    = { cron = "cron(0 9 * * ? *)", input = { mode = "health" } } # 유지보수(03:30) 후
  }
  name                         = "${local.name}-monitor-${each.key}"
  schedule_expression          = each.value.cron
  schedule_expression_timezone = "Asia/Seoul"
  flexible_time_window {
    mode = "OFF"
  }
  target {
    arn      = aws_lambda_function.monitor.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode(each.value.input)
  }
}

# ---------------------------------------------------------------------------
# 알람: slos.yaml 의 SLO·증상. 설명에 목표와 런북 링크를 넣는다
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_metric_alarm" "this" {
  for_each            = local.alarms
  alarm_name          = "${local.name}-${each.key}"
  alarm_description   = "[${each.value.kind}] ${lookup(each.value, "objective", "")} | 런북: ${local.runbook}/${each.value.runbook}"
  namespace           = each.value.metric.namespace
  metric_name         = each.value.metric.name
  dimensions          = each.value.metric.dimensions
  statistic           = each.value.statistic
  period              = each.value.alarm.period
  evaluation_periods  = each.value.alarm.periods
  threshold           = each.value.alarm.threshold
  comparison_operator = each.value.alarm.comparison
  # 측정이 멈춘 것도 장애다: 신선도·커넥터 지표가 안 들어오면 알람
  treat_missing_data = contains(["silver_orders_freshness", "bronze_ingest_freshness", "cdc_connector_down"], each.key) ? "breaching" : "notBreaching"
  alarm_actions      = [local.in["security/alerts_topic_arn"]]
  ok_actions         = [local.in["security/alerts_topic_arn"]]
}

resource "aws_cloudwatch_metric_alarm" "flink_checkpoints" {
  for_each            = toset([local.in["p1/processing/ingest_app_name"], local.in["p1/processing/realtime_app_name"]])
  alarm_name          = "${each.key}-checkpoint-failures"
  alarm_description   = "[증상] 체크포인트 실패 = Iceberg 커밋 중단(Bronze 정지) | 런북: ${local.runbook}/ingest-stalled.md"
  namespace           = "AWS/KinesisAnalytics"
  metric_name         = "numberOfFailedCheckpoints"
  dimensions          = { Application = each.key }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [local.in["security/alerts_topic_arn"]]
}

# ---------------------------------------------------------------------------
# 대시보드: 위에서 아래로 "사용자 영향 → 파이프라인 → 인프라"
# ---------------------------------------------------------------------------
locals {
  fresh_metrics = [for t in local.cfg.monitored_tables : ["Danbi/Freshness", "FreshnessSeconds", "Table", t]]
}

resource "aws_cloudwatch_dashboard" "p1" {
  dashboard_name = "${local.name}-p1"
  dashboard_body = jsonencode({
    widgets = [
      { type = "text", x = 0, y = 0, width = 24, height = 2, properties = { markdown = "## 단비 P1 — 데이터 제품 상태\n실시간 매출은 **속보치**(확정치는 다음 날 07:00 Gold). 알림마다 런북 링크가 있다." } },
      { type = "metric", x = 0, y = 2, width = 12, height = 6, properties = { title = "실시간 순매출(1분, 속보치)", region = var.region, stat = "Sum", period = 60, metrics = [["Danbi/Realtime", "NetSales1m"], [".", "ApprovedAmount1m"]] } },
      { type = "metric", x = 12, y = 2, width = 12, height = 6, properties = { title = "대사: 설명되지 않은 차이(목표 0)", region = var.region, stat = "Maximum", period = 300, metrics = [["Danbi/Recon", "UnexplainedCount", "Feed", "pg_settlement"], [".", "PendingCount", ".", "."], [".", "SourceLakeMismatch", ".", "cdc"]] } },
      { type = "metric", x = 0, y = 8, width = 24, height = 6, properties = { title = "신선도(초) — 계층별", region = var.region, stat = "Maximum", period = 300, metrics = local.fresh_metrics, annotations = { horizontal = [{ label = "Silver SLO 15분", value = 900 }] } } },
      { type = "metric", x = 0, y = 14, width = 8, height = 6, properties = { title = "수집 컨슈머 지연(초)", region = var.region, stat = "Maximum", period = 60, metrics = [["AWS/Kafka", "EstimatedMaxTimeLag", "Consumer Group", "flink-bronze-cdc"], ["...", "flink-bronze-clicks"], ["...", "flink-bronze-events"]] } },
      { type = "metric", x = 8, y = 14, width = 8, height = 6, properties = { title = "Flink 체크포인트", region = var.region, stat = "Maximum", period = 300, metrics = [["AWS/KinesisAnalytics", "lastCheckpointDuration", "Application", local.in["p1/processing/ingest_app_name"]], [".", "numberOfFailedCheckpoints", ".", "."]] } },
      { type = "metric", x = 16, y = 14, width = 8, height = 6, properties = { title = "커넥터·DLQ·격리 파일", region = var.region, stat = "Sum", period = 300, metrics = [["Danbi/Pipeline", "ConnectorRunning", { stat = "Minimum" }], [".", "DlqMessages", "Topic", "dlq.ingest"], ["Danbi/Recon", "QuarantinedFiles", "Feed", "pg_settlement"]] } },
      { type = "metric", x = 0, y = 20, width = 12, height = 6, properties = { title = "이상결제 탐지·null 금액(F-08 조기 신호)", region = var.region, stat = "Sum", period = 300, metrics = [["Danbi/Realtime", "NullAmounts1m"], [".", "Payments1m"]] } },
      { type = "metric", x = 12, y = 20, width = 12, height = 6, properties = { title = "작은 파일 비율(NFR-11 ≤ 10%)", region = var.region, stat = "Maximum", period = 86400, metrics = [for t in local.cfg.small_file_tables : ["Danbi/Lake", "SmallFileRatio", "Table", t]] } },
    ]
  })
}

module "outputs" {
  source = "../../modules/ssm_outputs"
  prefix = "${local.ssm_prefix}/p1/observability"
  values = {
    dashboard   = aws_cloudwatch_dashboard.p1.dashboard_name
    monitor_fn  = aws_lambda_function.monitor.function_name
    alarm_count = tostring(length(local.alarms))
  }
}
