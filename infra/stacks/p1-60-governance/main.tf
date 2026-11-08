# P1-12 거버넌스: Lake Formation(태그 기반 접근 제어, 행 필터), 비상 접근(break-glass), 기간제 접근 요청
#
# 3중 통제
#   1) 카탈로그: LF 태그 식으로 DB·테이블·컬럼 권한(이 스택)
#   2) 질의: Athena 워크그룹(결과 위치·스캔 상한 강제, p1-30)
#   3) 저장소: 사람 역할에는 레이크 버킷 S3 권한이 없다. LF 가 발급한 임시 자격증명으로만 읽는다
# 쓰기(MERGE·INSERT OVERWRITE·컴팩션)는 LF 가 통제하지 않는다 → 쓰기 주체를 파이프라인 역할로 한정(ADR AWS-06)

variable "lf_admin_arns" {
  description = "LF 관리자 추가(엔지니어 역할 등). Terraform 실행 역할은 자동 포함"
  type        = list(string)
  default     = []
}

variable "create_data_filters" {
  description = "gold.dim_member 가 게시된 뒤 true(행 필터는 테이블이 있어야 만들 수 있다)"
  type        = bool
  default     = false
}

data "aws_iam_session_context" "current" {
  arn = data.aws_caller_identity.current.arn
}

data "aws_ssm_parameter" "in" {
  for_each = toset([
    "security/kms_lake_arn", "security/kms_pii_arn", "security/alerts_topic_arn",
    "security/role_analyst_arn", "security/role_finance_arn", "security/role_engineer_arn",
    "p1/lakehouse/silver_bucket_arn", "p1/lakehouse/gold_bucket_arn", "p1/lakehouse/workgroup_engineering",
    "p1/lakehouse/recon_task_role_arn", "p1/processing/emr_job_role_arn", "p1/processing/dbt_task_role_arn",
    "p1/processing/member_key_table", "p1/orchestration/mwaa_role_arn",
  ])
  name = "${local.ssm_prefix}/${each.key}"
}

locals {
  in       = { for k, p in data.aws_ssm_parameter.in : k => p.value }
  analyst  = local.in["security/role_analyst_arn"]
  finance  = local.in["security/role_finance_arn"]
  engineer = local.in["security/role_engineer_arn"]
  pipelines = {
    emr   = local.in["p1/processing/emr_job_role_arn"]
    dbt   = local.in["p1/processing/dbt_task_role_arn"]
    recon = local.in["p1/lakehouse/recon_task_role_arn"]
    mwaa  = local.in["p1/orchestration/mwaa_role_arn"]
  }
  governed_dbs = ["silver", "gold", "ops", "gold_audit"]
}

# ---------------------------------------------------------------------------
# 1. LF 설정: 새 DB·테이블에 IAMAllowedPrincipals 기본 권한을 주지 않는다(= LF 모드)
#    기존 리소스의 기본 권한 회수는 1회 스크립트: python -m p1.governance.lf_sync revoke-iam-allowed
# ---------------------------------------------------------------------------
resource "aws_lakeformation_data_lake_settings" "this" {
  admins = distinct(concat([data.aws_iam_session_context.current.issuer_arn], var.lf_admin_arns))
  # create_*_default_permissions 블록을 두지 않으면 기본 권한이 빈 목록이 된다(IAMAllowedPrincipals 미부여)
  allow_full_table_external_data_access = false
}

# 등록 위치용 역할(서비스 연결 역할 대신: KMS 고객 관리형 키 권한을 줄 수 있어야 한다)
data "aws_iam_policy_document" "lf_trust" {
  statement {
    actions = ["sts:AssumeRole", "sts:SetContext"]
    principals {
      type        = "Service"
      identifiers = ["lakeformation.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lf_access" {
  name               = "${local.name}-lf-access"
  assume_role_policy = data.aws_iam_policy_document.lf_trust.json
}

data "aws_iam_policy_document" "lf_access" {
  statement {
    actions = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket", "s3:GetBucketLocation"]
    resources = [
      local.in["p1/lakehouse/silver_bucket_arn"], "${local.in["p1/lakehouse/silver_bucket_arn"]}/*",
      local.in["p1/lakehouse/gold_bucket_arn"], "${local.in["p1/lakehouse/gold_bucket_arn"]}/*",
    ]
  }
  statement {
    actions   = ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey"]
    resources = [local.in["security/kms_lake_arn"]]
  }
}

resource "aws_iam_role_policy" "lf_access" {
  name   = "lf-access"
  role   = aws_iam_role.lf_access.id
  policy = data.aws_iam_policy_document.lf_access.json
}

resource "aws_lakeformation_resource" "lake" {
  for_each = {
    silver = local.in["p1/lakehouse/silver_bucket_arn"]
    gold   = local.in["p1/lakehouse/gold_bucket_arn"]
  }
  arn                   = each.value
  role_arn              = aws_iam_role.lf_access.arn
  hybrid_access_enabled = false
}

# ---------------------------------------------------------------------------
# 2. LF 태그(값 목록이 곧 분류 체계). 테이블·컬럼 태그는 p1/governance/lf_sync.py 가 계약·dbt 에서 동기화
# ---------------------------------------------------------------------------
resource "aws_lakeformation_lf_tag" "layer" {
  key    = "layer"
  values = ["silver", "gold", "ops", "gold_audit"]
}

resource "aws_lakeformation_lf_tag" "domain" {
  key    = "domain"
  values = ["commerce", "payment", "marketing", "cs", "finance", "platform"]
}

resource "aws_lakeformation_lf_tag" "classification" {
  key    = "classification"
  values = ["public", "internal", "personal_token", "personal_encrypted", "personal_generalized", "restricted"]
}

resource "aws_lakeformation_resource_lf_tags" "db_layer" {
  for_each = toset(local.governed_dbs)
  database {
    name = each.key
  }
  lf_tag {
    key   = aws_lakeformation_lf_tag.layer.key
    value = each.key
  }
}

# ---------------------------------------------------------------------------
# 3. 사람 역할: 태그 식 권한
# ---------------------------------------------------------------------------
locals {
  human_grants = {
    analyst = {
      principal = local.analyst
      layer     = ["gold"]
      classes   = ["public", "internal", "personal_generalized"]
    }
    finance = {
      principal = local.finance
      layer     = ["gold"]
      classes   = ["public", "internal"]
    }
    engineer = {
      principal = local.engineer
      layer     = ["silver", "gold", "ops"]
      classes   = ["public", "internal", "personal_token", "personal_generalized"] # 암호문·restricted 제외
    }
  }
}

resource "aws_lakeformation_permissions" "human_db" {
  for_each    = local.human_grants
  principal   = each.value.principal
  permissions = ["DESCRIBE"]
  lf_tag_policy {
    resource_type = "DATABASE"
    expression {
      key    = aws_lakeformation_lf_tag.layer.key
      values = each.value.layer
    }
  }
}

resource "aws_lakeformation_permissions" "human_table" {
  for_each    = local.human_grants
  principal   = each.value.principal
  permissions = ["SELECT", "DESCRIBE"]
  lf_tag_policy {
    resource_type = "TABLE"
    expression {
      key    = aws_lakeformation_lf_tag.layer.key
      values = each.value.layer
    }
    expression {
      key    = aws_lakeformation_lf_tag.classification.key
      values = each.value.classes
    }
  }
}

# ---------------------------------------------------------------------------
# 4. 파이프라인 역할: 위치 접근 + 이름 기반 전체 권한(쓰기 주체)
# ---------------------------------------------------------------------------
locals {
  pipeline_db = { for p in setproduct(keys(local.pipelines), local.governed_dbs) : "${p[0]}-${p[1]}" => { role = local.pipelines[p[0]], db = p[1] } }
}

resource "aws_lakeformation_permissions" "pipeline_location" {
  for_each    = { for p in setproduct(keys(local.pipelines), keys(aws_lakeformation_resource.lake)) : "${p[0]}-${p[1]}" => { role = local.pipelines[p[0]], arn = aws_lakeformation_resource.lake[p[1]].arn } }
  principal   = each.value.role
  permissions = ["DATA_LOCATION_ACCESS"]
  data_location {
    arn = each.value.arn
  }
}

resource "aws_lakeformation_permissions" "pipeline_db" {
  for_each    = local.pipeline_db
  principal   = each.value.role
  permissions = ["CREATE_TABLE", "ALTER", "DESCRIBE"]
  database {
    name = each.value.db
  }
}

resource "aws_lakeformation_permissions" "pipeline_tables" {
  for_each    = local.pipeline_db
  principal   = each.value.role
  permissions = ["ALL"]
  table {
    database_name = each.value.db
    wildcard      = true
  }
}

# ---------------------------------------------------------------------------
# 5. 행 필터: 분석가는 dim_member(restricted)를 '마케팅 동의 회원' 행만, 토큰 컬럼 없이 본다
# ---------------------------------------------------------------------------
resource "aws_lakeformation_data_cells_filter" "consented_members" {
  count = var.create_data_filters ? 1 : 0
  table_data {
    database_name    = "gold"
    table_name       = "dim_member"
    name             = "consented_members"
    table_catalog_id = local.account_id
    row_filter {
      filter_expression = "marketing_agreed = true"
    }
    column_wildcard {
      excluded_column_names = ["member_token"]
    }
  }
}

resource "aws_lakeformation_permissions" "analyst_consented_members" {
  count       = var.create_data_filters ? 1 : 0
  principal   = local.analyst
  permissions = ["SELECT"]
  data_cells_filter {
    database_name    = "gold"
    table_name       = "dim_member"
    name             = "consented_members"
    table_catalog_id = local.account_id
  }
  depends_on = [aws_lakeformation_data_cells_filter.consented_members]
}

# ---------------------------------------------------------------------------
# 6. 비상 접근(break-glass): MFA + SourceIdentity(누가) 필수, 1시간, 사용 즉시 알림, 사후 검토
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "breakglass_trust" {
  statement {
    actions = ["sts:AssumeRole", "sts:SetSourceIdentity"]
    principals {
      type        = "AWS"
      identifiers = [local.engineer]
    }
    condition {
      test     = "Bool"
      variable = "aws:MultiFactorAuthPresent"
      values   = ["true"]
    }
    condition {
      test     = "StringLike"
      variable = "sts:SourceIdentity"
      values   = ["*@*"] # 사람의 이메일. CloudTrail 에 남는다
    }
  }
}

resource "aws_iam_role" "breakglass" {
  name                 = "${local.name}-breakglass"
  assume_role_policy   = data.aws_iam_policy_document.breakglass_trust.json
  max_session_duration = 3600
}

data "aws_iam_policy_document" "breakglass" {
  statement {
    actions   = ["athena:StartQueryExecution", "athena:GetQueryExecution", "athena:GetQueryResults", "athena:GetWorkGroup"]
    resources = ["arn:aws:athena:${var.region}:${local.account_id}:workgroup/${local.in["p1/lakehouse/workgroup_engineering"]}"]
  }
  statement {
    actions   = ["glue:GetDatabase", "glue:GetDatabases", "glue:GetTable", "glue:GetTables", "lakeformation:GetDataAccess"]
    resources = ["*"]
  }
  statement {
    sid       = "DecryptMemberFields" # 암호문 컬럼 복호화(CS 분쟁 대응 등): 회원 키 + KMS
    actions   = ["dynamodb:GetItem"]
    resources = ["arn:aws:dynamodb:${var.region}:${local.account_id}:table/${local.in["p1/processing/member_key_table"]}"]
  }
  statement {
    actions   = ["kms:Decrypt"]
    resources = [local.in["security/kms_pii_arn"]]
  }
}

resource "aws_iam_role_policy" "breakglass" {
  name   = "breakglass"
  role   = aws_iam_role.breakglass.id
  policy = data.aws_iam_policy_document.breakglass.json
}

resource "aws_lakeformation_permissions" "breakglass" {
  principal   = aws_iam_role.breakglass.arn
  permissions = ["SELECT", "DESCRIBE"]
  lf_tag_policy {
    resource_type = "TABLE"
    expression {
      key    = aws_lakeformation_lf_tag.layer.key
      values = ["silver", "gold"]
    }
  }
}

resource "aws_cloudwatch_event_rule" "breakglass_used" {
  name        = "${local.name}-breakglass-used"
  description = "비상 접근 역할 사용 즉시 알림"
  event_pattern = jsonencode({
    source      = ["aws.sts"]
    detail-type = ["AWS API Call via CloudTrail"]
    detail = {
      eventName         = ["AssumeRole"]
      requestParameters = { roleArn = [aws_iam_role.breakglass.arn] }
    }
  })
}

resource "aws_cloudwatch_event_target" "breakglass_used" {
  rule = aws_cloudwatch_event_rule.breakglass_used.name
  arn  = local.in["security/alerts_topic_arn"]
}

# ---------------------------------------------------------------------------
# 7. 기간제 접근 요청(S-G2): governance/access/*.yaml. 만료되면 다음 apply(매일 예약 워크플로)에서 회수된다
# ---------------------------------------------------------------------------
locals {
  access_files = fileset("${path.module}/../../../governance/access", "*.yaml")
  access_requests = {
    for f in local.access_files : trimsuffix(f, ".yaml") => yamldecode(file("${path.module}/../../../governance/access/${f}"))
  }
  active_requests = { for k, r in local.access_requests : k => r if timecmp(timestamp(), r.expires_at) < 0 }
}

resource "aws_lakeformation_permissions" "access_request" {
  for_each    = local.active_requests
  principal   = each.value.principal_arn
  permissions = ["SELECT", "DESCRIBE"]
  lf_tag_policy {
    resource_type = "TABLE"
    expression {
      key    = aws_lakeformation_lf_tag.layer.key
      values = each.value.scope.layer
    }
    expression {
      key    = aws_lakeformation_lf_tag.classification.key
      values = each.value.scope.classification
    }
  }
}

module "outputs" {
  source = "../../modules/ssm_outputs"
  prefix = "${local.ssm_prefix}/p1/governance"
  values = {
    lf_access_role_arn = aws_iam_role.lf_access.arn
    breakglass_role    = aws_iam_role.breakglass.arn
    active_requests    = join(",", keys(local.active_requests))
  }
}

output "active_access_requests" {
  value = { for k, r in local.active_requests : k => r.expires_at }
}
