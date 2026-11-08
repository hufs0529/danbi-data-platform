# P1-08 dbt 실행기(ECS RunTask). Athena 로 gold_audit 에 쓰고, 게시는 EMR(publish_gold)이 한다.

variable "dbt_image_tag" {
  type    = string
  default = "0.1.0"
}

data "aws_ssm_parameter" "platform" {
  for_each = toset(["ecs_cluster_arn", "ecs_execution_role_arn", "ecs_log_group", "ecr_registry", "ecr_prefix"])
  name     = "${local.ssm_prefix}/platform/${each.key}"
}

data "aws_ssm_parameter" "lake_extra" {
  for_each = toset(["athena_bucket", "workgroup_pipeline"])
  name     = "${local.ssm_prefix}/p1/lakehouse/${each.key}"
}

locals {
  platform = { for k, p in data.aws_ssm_parameter.platform : k => p.value }
  lake_x   = { for k, p in data.aws_ssm_parameter.lake_extra : k => p.value }
}

data "aws_iam_policy_document" "dbt" {
  statement {
    sid = "Athena"
    actions = [
      "athena:StartQueryExecution", "athena:GetQueryExecution", "athena:GetQueryResults",
      "athena:StopQueryExecution", "athena:GetWorkGroup", "athena:ListDataCatalogs", "athena:GetDataCatalog",
    ]
    resources = [
      "arn:aws:athena:${var.region}:${local.account_id}:workgroup/${local.lake_x["workgroup_pipeline"]}",
      "arn:aws:athena:${var.region}:${local.account_id}:datacatalog/AwsDataCatalog",
    ]
  }
  statement {
    sid = "GlueRead"
    actions = ["glue:GetDatabase", "glue:GetDatabases", "glue:GetTable", "glue:GetTables", "glue:GetPartitions",
    "glue:BatchGetPartition"]
    resources = [
      "arn:aws:glue:${var.region}:${local.account_id}:catalog",
      "arn:aws:glue:${var.region}:${local.account_id}:database/*",
      "arn:aws:glue:${var.region}:${local.account_id}:table/silver/*",
      "arn:aws:glue:${var.region}:${local.account_id}:table/gold_audit/*",
      "arn:aws:glue:${var.region}:${local.account_id}:table/gold/*",
    ]
  }
  statement {
    sid       = "GlueWriteAudit" # dbt 는 gold_audit 에만 쓴다. gold 쓰기는 게시 잡만
    actions   = ["glue:CreateTable", "glue:UpdateTable", "glue:DeleteTable", "glue:BatchDeleteTable"]
    resources = ["arn:aws:glue:${var.region}:${local.account_id}:catalog", "arn:aws:glue:${var.region}:${local.account_id}:database/gold_audit", "arn:aws:glue:${var.region}:${local.account_id}:table/gold_audit/*"]
  }
  statement {
    sid       = "S3Read"
    actions   = ["s3:GetObject", "s3:ListBucket", "s3:GetBucketLocation"]
    resources = ["arn:aws:s3:::${local.in["p1/lakehouse/silver_bucket"]}", "arn:aws:s3:::${local.in["p1/lakehouse/silver_bucket"]}/*"]
  }
  statement {
    sid     = "S3Write"
    actions = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket", "s3:GetBucketLocation", "s3:AbortMultipartUpload"]
    resources = [
      "arn:aws:s3:::${local.in["p1/lakehouse/gold_bucket"]}", "arn:aws:s3:::${local.in["p1/lakehouse/gold_bucket"]}/_audit/*",
      "arn:aws:s3:::${local.lake_x["athena_bucket"]}", "arn:aws:s3:::${local.lake_x["athena_bucket"]}/*",
      "arn:aws:s3:::${local.artifacts}/dbt/*",
    ]
  }
  statement {
    sid       = "LakeKey"
    actions   = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey"]
    resources = [local.in["security/kms_lake_arn"]]
  }
  statement {
    sid       = "LakeFormation"
    actions   = ["lakeformation:GetDataAccess"]
    resources = ["*"]
  }
}

module "dbt_runner" {
  source             = "../../modules/ecs_service"
  name               = "${local.name}-dbt-runner"
  cluster_arn        = local.platform.ecs_cluster_arn
  image              = "${local.platform.ecr_registry}/${local.platform.ecr_prefix}/dbt-runner:${var.dbt_image_tag}"
  cpu                = 1024
  memory             = 2048
  command            = ["build", "--select", "tag:publish"]
  execution_role_arn = local.platform.ecs_execution_role_arn
  log_group_name     = local.platform.ecs_log_group
  task_policy_json   = data.aws_iam_policy_document.dbt.json
  environment = {
    AWS_REGION       = var.region
    ATHENA_WORKGROUP = local.lake_x["workgroup_pipeline"]
    ATHENA_BUCKET    = local.lake_x["athena_bucket"]
    GOLD_BUCKET      = local.in["p1/lakehouse/gold_bucket"]
    ARTIFACTS_BUCKET = local.artifacts
  }
  create_service = false
}

module "dbt_outputs" {
  source = "../../modules/ssm_outputs"
  prefix = "${local.ssm_prefix}/p1/processing"
  values = {
    dbt_task_family   = module.dbt_runner.task_definition_family
    dbt_task_role_arn = module.dbt_runner.task_role_arn
  }
}
