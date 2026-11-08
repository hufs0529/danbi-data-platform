variable "alert_email" {
  description = "운영 알림(SNS) 구독 이메일. 비우면 구독을 만들지 않는다"
  type        = string
  default     = ""
}

variable "access_log_retention_days" {
  description = "접속기록 보관 일수. 5만 명 이상 처리 시 2년 이상 (안전성 확보조치 기준 제8조)"
  type        = number
  default     = 730
}

variable "audit_lock_mode" {
  description = "GOVERNANCE: 특권 역할이 우회 가능(dev 정리용). 운영은 COMPLIANCE"
  type        = string
  default     = "GOVERNANCE"
}

data "aws_iam_policy_document" "kms_base" {
  statement {
    sid       = "AccountAdmin"
    actions   = ["kms:*"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${local.account_id}:root"]
    }
  }
}

# ---------------------------------------------------------------------------
# KMS 키: 용도별로 나눠 키 정책과 감사 범위를 분리한다
#   lake : 레이크 버킷(S3), Glue, Athena 결과
#   pii  : PII 토큰화 비밀, 회원별 데이터 키(crypto-shredding)
#   logs : CloudTrail, CloudWatch Logs, SNS
# ---------------------------------------------------------------------------
resource "aws_kms_key" "lake" {
  description             = "${local.name} lake data"
  enable_key_rotation     = true
  deletion_window_in_days = 7
  policy                  = data.aws_iam_policy_document.kms_base.json
}

resource "aws_kms_alias" "lake" {
  name          = "alias/${local.name}-lake"
  target_key_id = aws_kms_key.lake.key_id
}

resource "aws_kms_key" "pii" {
  description             = "${local.name} PII tokenization and member data keys"
  enable_key_rotation     = true
  deletion_window_in_days = 7
  policy                  = data.aws_iam_policy_document.kms_base.json
}

resource "aws_kms_alias" "pii" {
  name          = "alias/${local.name}-pii"
  target_key_id = aws_kms_key.pii.key_id
}

data "aws_iam_policy_document" "kms_logs" {
  source_policy_documents = [data.aws_iam_policy_document.kms_base.json]

  statement {
    sid       = "CloudTrailEncrypt"
    actions   = ["kms:GenerateDataKey*", "kms:DescribeKey"]
    resources = ["*"]
    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }

  statement {
    sid       = "CloudWatchLogs"
    actions   = ["kms:Encrypt*", "kms:Decrypt*", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"]
    resources = ["*"]
    principals {
      type        = "Service"
      identifiers = ["logs.${var.region}.amazonaws.com"]
    }
    condition {
      test     = "ArnLike"
      variable = "kms:EncryptionContext:aws:logs:arn"
      values   = ["arn:aws:logs:${var.region}:${local.account_id}:*"]
    }
  }

  # CloudWatch 알람·EventBridge 가 KMS 암호화된 SNS 토픽에 게시할 수 있어야 한다
  statement {
    sid       = "AlarmsToEncryptedSns"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey*"]
    resources = ["*"]
    principals {
      type        = "Service"
      identifiers = ["cloudwatch.amazonaws.com", "events.amazonaws.com"]
    }
  }
}

resource "aws_kms_key" "logs" {
  description             = "${local.name} logs and alerts"
  enable_key_rotation     = true
  deletion_window_in_days = 7
  policy                  = data.aws_iam_policy_document.kms_logs.json
}

resource "aws_kms_alias" "logs" {
  name          = "alias/${local.name}-logs"
  target_key_id = aws_kms_key.logs.key_id
}

# ---------------------------------------------------------------------------
# 감사 버킷: 객체 잠금(WORM) + KMS. 접속기록 위·변조 방지 요건을 저장소 수준에서 만족시킨다
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "audit_cloudtrail" {
  statement {
    sid     = "CloudTrailAclCheck"
    actions = ["s3:GetBucketAcl"]
    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }
    resources = ["arn:aws:s3:::${local.name}-audit-${local.account_id}"]
    condition {
      test     = "StringEquals"
      variable = "aws:SourceArn"
      values   = ["arn:aws:cloudtrail:${var.region}:${local.account_id}:trail/${local.name}-trail"]
    }
  }
  statement {
    sid     = "CloudTrailWrite"
    actions = ["s3:PutObject"]
    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }
    resources = ["arn:aws:s3:::${local.name}-audit-${local.account_id}/cloudtrail/AWSLogs/${local.account_id}/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:SourceArn"
      values   = ["arn:aws:cloudtrail:${var.region}:${local.account_id}:trail/${local.name}-trail"]
    }
    condition {
      test     = "StringEquals"
      variable = "s3:x-amz-acl"
      values   = ["bucket-owner-full-control"]
    }
  }
}

module "audit_bucket" {
  source           = "../../modules/secure_bucket"
  name             = "${local.name}-audit-${local.account_id}"
  kms_key_arn      = aws_kms_key.logs.arn
  versioning       = true
  object_lock      = { mode = var.audit_lock_mode, days = var.access_log_retention_days }
  policy_documents = [data.aws_iam_policy_document.audit_cloudtrail.json]
}

# ---------------------------------------------------------------------------
# CloudTrail
#   - 관리 이벤트 전부 (Lake Formation 권한 변경, Athena 쿼리 실행, Cognito 그룹 변경 포함)
#   - 데이터 이벤트: 개인정보가 있는 레이크 버킷(silver, gold, kl-raw)의 객체 읽기·쓰기
#   - 로그 파일 무결성 검증(digest) 켜기 → 접속기록 위·변조 여부를 증명할 수 있다
# ---------------------------------------------------------------------------
resource "aws_cloudtrail" "this" {
  name                          = "${local.name}-trail"
  s3_bucket_name                = module.audit_bucket.id
  s3_key_prefix                 = "cloudtrail"
  kms_key_id                    = aws_kms_key.logs.arn
  include_global_service_events = true
  is_multi_region_trail         = false
  enable_log_file_validation    = true

  advanced_event_selector {
    name = "management-events"
    field_selector {
      field  = "eventCategory"
      equals = ["Management"]
    }
  }

  advanced_event_selector {
    name = "pii-bucket-object-access"
    field_selector {
      field  = "eventCategory"
      equals = ["Data"]
    }
    field_selector {
      field  = "resources.type"
      equals = ["AWS::S3::Object"]
    }
    field_selector {
      field = "resources.ARN"
      starts_with = [
        "arn:aws:s3:::${local.name}-silver-${local.account_id}/",
        "arn:aws:s3:::${local.name}-gold-${local.account_id}/",
        "arn:aws:s3:::${local.name}-kl-raw-${local.account_id}/",
      ]
    }
  }

  depends_on = [module.audit_bucket]
}

# ---------------------------------------------------------------------------
# 운영 알림 토픽
# ---------------------------------------------------------------------------
resource "aws_sns_topic" "alerts" {
  name              = "${local.name}-alerts"
  kms_master_key_id = aws_kms_key.logs.arn
}

resource "aws_sns_topic_subscription" "alerts_email" {
  count     = var.alert_email == "" ? 0 : 1
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# ---------------------------------------------------------------------------
# PII 토큰화 비밀: 값은 Terraform state 에 남기지 않으려고 scripts/init_pii_secret.py 로 넣는다
# ---------------------------------------------------------------------------
resource "aws_secretsmanager_secret" "pii_hmac" {
  name                    = "${local.name}/pii/hmac-key"
  description             = "결정적 토큰화(HMAC-SHA256) 키. 값은 init_pii_secret.py 로 생성"
  kms_key_id              = aws_kms_key.pii.arn
  recovery_window_in_days = 7
}

# ---------------------------------------------------------------------------
# 사람용 역할: 분석가·엔지니어·재무. 콘솔/CLI 에서 MFA 로 가정한다
# 데이터 권한(테이블·열·행)은 Lake Formation(P1-12)이 준다. 여기서는 서비스 접근만
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "human_trust" {
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

data "aws_iam_policy_document" "query_access" {
  statement {
    sid = "AthenaAndCatalogRead"
    actions = [
      "athena:StartQueryExecution", "athena:GetQueryExecution", "athena:GetQueryResults",
      "athena:StopQueryExecution", "athena:GetWorkGroup", "athena:ListQueryExecutions",
      "glue:GetDatabase", "glue:GetDatabases", "glue:GetTable", "glue:GetTables",
      "glue:GetPartition", "glue:GetPartitions",
      "lakeformation:GetDataAccess",
    ]
    resources = ["*"]
  }
}

locals {
  human_roles = {
    analyst  = "분석가: 마스킹된 Gold·Silver 조회"
    finance  = "재무: 매출·대사 테이블 조회"
    engineer = "데이터 엔지니어: 전 계층 조회, 파이프라인 운영"
  }
}

resource "aws_iam_role" "human" {
  for_each             = local.human_roles
  name                 = "${local.name}-${each.key}"
  description          = each.value
  assume_role_policy   = data.aws_iam_policy_document.human_trust.json
  max_session_duration = 14400
}

resource "aws_iam_role_policy" "human_query" {
  for_each = aws_iam_role.human
  name     = "athena-query"
  role     = each.value.id
  policy   = data.aws_iam_policy_document.query_access.json
}

# ---------------------------------------------------------------------------
# Session Manager: SSH 대신 SSM. 세션 로그를 감사 버킷에 남긴다(운영자 접속기록)
# ---------------------------------------------------------------------------
resource "aws_ssm_document" "session_prefs" {
  name            = "SSM-SessionManagerRunShell"
  document_type   = "Session"
  document_format = "JSON"
  content = jsonencode({
    schemaVersion = "1.0"
    description   = "Session Manager preferences"
    sessionType   = "Standard_Stream"
    inputs = {
      s3BucketName           = module.audit_bucket.id
      s3KeyPrefix            = "ssm-sessions"
      s3EncryptionEnabled    = true
      kmsKeyId               = aws_kms_key.logs.arn
      idleSessionTimeout     = "20"
      runAsEnabled           = false
      shellProfile           = { linux = "export HISTTIMEFORMAT='%F %T '" }
      cloudWatchLogGroupName = ""
    }
  })
}

module "outputs" {
  source = "../../modules/ssm_outputs"
  prefix = "${local.ssm_prefix}/security"
  values = {
    kms_lake_arn        = aws_kms_key.lake.arn
    kms_pii_arn         = aws_kms_key.pii.arn
    kms_logs_arn        = aws_kms_key.logs.arn
    audit_bucket        = module.audit_bucket.id
    alerts_topic_arn    = aws_sns_topic.alerts.arn
    pii_hmac_secret_arn = aws_secretsmanager_secret.pii_hmac.arn
    role_analyst_arn    = aws_iam_role.human["analyst"].arn
    role_finance_arn    = aws_iam_role.human["finance"].arn
    role_engineer_arn   = aws_iam_role.human["engineer"].arn
  }
}

output "alerts_topic_arn" {
  value = aws_sns_topic.alerts.arn
}

output "audit_bucket" {
  value = module.audit_bucket.id
}
