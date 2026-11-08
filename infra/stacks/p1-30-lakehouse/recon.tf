# P1-09 정산 파일 도착 감지와 대사 잡
#   S3(landing, pg_settlement/) ─EventBridge─> SQS ─> MWAA AssetWatcher(P1-10) ─> ECS RunTask(load-settlement)

variable "recon_image_tag" {
  type    = string
  default = "0.1.0"
}

data "aws_ssm_parameter" "plat" {
  for_each = toset([
    "platform/ecs_cluster_arn", "platform/ecs_execution_role_arn", "platform/ecs_log_group", "platform/ecr_registry",
    "platform/ecr_prefix", "security/kms_logs_arn", "p1/sources/db_user_secret_prefix",
  ])
  name = "${local.ssm_prefix}/${each.key}"
}

locals {
  plat = { for k, p in data.aws_ssm_parameter.plat : k => p.value }
}

# ---------------------------------------------------------------------------
# 도착 이벤트 큐. 같은 파일이 두 번 와도(재전송) 이벤트는 두 번 → 적재기가 해시로 판정한다
# ---------------------------------------------------------------------------
# 메시지는 S3 이벤트 메타데이터(버킷·키)뿐이라 SQS 관리형 암호화로 충분하다.
# 고객 관리형 KMS 를 쓰면 키 정책에 events.amazonaws.com 을 열어야 한다(KMS grant 는 서비스 주체에 줄 수 없다)
resource "aws_sqs_queue" "settlement_dlq" {
  name                      = "${local.name}-pg-settlement-arrivals-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "settlement" {
  name                       = "${local.name}-pg-settlement-arrivals"
  visibility_timeout_seconds = 900
  message_retention_seconds  = 345600
  sqs_managed_sse_enabled    = true
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.settlement_dlq.arn
    maxReceiveCount     = 5
  })
}

resource "aws_cloudwatch_event_rule" "settlement_arrival" {
  name        = "${local.name}-pg-settlement-arrival"
  description = "PG 정산 파일이 랜딩 버킷에 도착"
  event_pattern = jsonencode({
    source      = ["aws.s3"]
    detail-type = ["Object Created"]
    detail = {
      bucket = { name = [module.bucket["landing"].id] }
      object = { key = [{ prefix = "pg_settlement/" }] }
    }
  })
}

resource "aws_cloudwatch_event_target" "settlement_arrival" {
  rule = aws_cloudwatch_event_rule.settlement_arrival.name
  arn  = aws_sqs_queue.settlement.arn
}

data "aws_iam_policy_document" "settlement_queue" {
  statement {
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.settlement.arn]
    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [aws_cloudwatch_event_rule.settlement_arrival.arn]
    }
  }
}

resource "aws_sqs_queue_policy" "settlement" {
  queue_url = aws_sqs_queue.settlement.id
  policy    = data.aws_iam_policy_document.settlement_queue.json
}

# ---------------------------------------------------------------------------
# 대사 잡 태스크 정의
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "recon" {
  statement {
    sid       = "Landing"
    actions   = ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject"]
    resources = ["${module.bucket["landing"].arn}/pg_settlement/*", "${module.bucket["landing"].arn}/quarantine/*"]
  }
  statement {
    sid     = "Lake"
    actions = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket", "s3:GetBucketLocation"]
    resources = [
      module.bucket["bronze"].arn, "${module.bucket["bronze"].arn}/pg_settlement_*",
      module.bucket["silver"].arn, "${module.bucket["silver"].arn}/*",
      module.bucket["gold"].arn, "${module.bucket["gold"].arn}/recon_pg_settlement/*", "${module.bucket["gold"].arn}/_reports/*",
      module.bucket["athena"].arn, "${module.bucket["athena"].arn}/pipeline/*",
    ]
  }
  statement {
    sid       = "Glue"
    actions   = ["glue:GetDatabase", "glue:GetTable", "glue:GetTables", "glue:UpdateTable", "glue:GetPartitions"]
    resources = ["arn:aws:glue:${var.region}:${local.account_id}:catalog", "arn:aws:glue:${var.region}:${local.account_id}:database/*", "arn:aws:glue:${var.region}:${local.account_id}:table/*/*"]
  }
  statement {
    sid       = "Athena"
    actions   = ["athena:StartQueryExecution", "athena:GetQueryExecution", "athena:GetQueryResults", "athena:GetWorkGroup"]
    resources = [aws_athena_workgroup.this["pipeline"].arn]
  }
  statement {
    sid       = "Key"
    actions   = ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey"]
    resources = [local.kms_lake]
  }
  statement {
    sid       = "ReconDbUsers"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = ["arn:aws:secretsmanager:${var.region}:${local.account_id}:secret:${local.plat["p1/sources/db_user_secret_prefix"]}*/recon-*"]
  }
  statement {
    sid       = "Metrics"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["Danbi/Recon"]
    }
  }
  statement {
    sid       = "LakeFormation"
    actions   = ["lakeformation:GetDataAccess"]
    resources = ["*"]
  }
}

module "recon" {
  source             = "../../modules/ecs_service"
  name               = "${local.name}-recon"
  cluster_arn        = local.plat["platform/ecs_cluster_arn"]
  image              = "${local.plat["platform/ecr_registry"]}/${local.plat["platform/ecr_prefix"]}/recon:${var.recon_image_tag}"
  cpu                = 1024
  memory             = 2048
  command            = ["recon-pg"]
  execution_role_arn = local.plat["platform/ecs_execution_role_arn"]
  log_group_name     = local.plat["platform/ecs_log_group"]
  task_policy_json   = data.aws_iam_policy_document.recon.json
  environment = {
    AWS_REGION            = var.region
    ATHENA_WORKGROUP      = aws_athena_workgroup.this["pipeline"].name
    GOLD_BUCKET           = module.bucket["gold"].id
    RECON_MYSQL_SECRET    = "${local.plat["p1/sources/db_user_secret_prefix"]}mysql/recon"
    RECON_POSTGRES_SECRET = "${local.plat["p1/sources/db_user_secret_prefix"]}postgres/recon"
  }
  create_service = false
}

module "recon_outputs" {
  source = "../../modules/ssm_outputs"
  prefix = "${local.ssm_prefix}/p1/lakehouse"
  values = {
    settlement_queue_url = aws_sqs_queue.settlement.id
    settlement_queue_arn = aws_sqs_queue.settlement.arn
    recon_task_family    = module.recon.task_definition_family
    recon_task_role_arn  = module.recon.task_role_arn
  }
}
