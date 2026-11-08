# P1-07 EMR Serverless: Silver MERGE(5~15분), Iceberg 유지보수(매일), Gold 게시(P1-08)
# 애플리케이션은 잡이 없을 때 과금되지 않는다(사전 용량 0). 최대 용량으로 비용 상한을 건다.

variable "emr_release" {
  type    = string
  default = "emr-7.12.0"
}

variable "spark_jobs_version" {
  description = "artifacts 버킷 spark/<version>/ (build_spark_jobs.sh --upload 의 git SHA)"
  type        = string
  default     = "dev"
}

resource "aws_security_group" "emr" {
  name        = "${local.name}-sg-emr"
  description = "EMR Serverless workers (egress only)"
  vpc_id      = local.in["network/vpc_id"]
}

resource "aws_vpc_security_group_egress_rule" "emr_all" {
  security_group_id = aws_security_group.emr.id
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
  description       = "S3 via gateway endpoint, Glue/STS via NAT"
}

resource "aws_emrserverless_application" "spark" {
  name          = "${local.name}-spark"
  release_label = var.emr_release
  type          = "spark"
  architecture  = "ARM64"

  maximum_capacity {
    cpu    = "16 vCPU"
    memory = "64 GB"
    disk   = "200 GB"
  }

  auto_start_configuration {
    enabled = true
  }

  auto_stop_configuration {
    enabled              = true
    idle_timeout_minutes = 10
  }

  scheduler_configuration {
    max_concurrent_runs   = 3 # MERGE 가 같은 테이블을 동시에 쓰면 커밋 충돌 → 재시도. 동시 실행 수를 묶는다
    queue_timeout_minutes = 60
  }

  network_configuration {
    subnet_ids         = local.app_subnet_ids
    security_group_ids = [aws_security_group.emr.id]
  }
}

data "aws_iam_policy_document" "emr_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["emr-serverless.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "emr_job" {
  name               = "${local.name}-emr-job"
  assume_role_policy = data.aws_iam_policy_document.emr_trust.json
}

locals {
  lake_buckets = [local.in["p1/lakehouse/bronze_bucket"], local.in["p1/lakehouse/silver_bucket"], local.in["p1/lakehouse/gold_bucket"]]
  glue_dbs     = ["bronze", "silver", "ops", "gold", "gold_audit", "rt"]
}

data "aws_iam_policy_document" "emr_job" {
  statement {
    sid       = "Lake"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket", "s3:GetBucketLocation"]
    resources = flatten([for b in local.lake_buckets : ["arn:aws:s3:::${b}", "arn:aws:s3:::${b}/*"]])
  }
  statement {
    sid       = "Jobs"
    actions   = ["s3:GetObject", "s3:ListBucket", "s3:PutObject"]
    resources = ["arn:aws:s3:::${local.artifacts}", "arn:aws:s3:::${local.artifacts}/spark/*", "arn:aws:s3:::${local.artifacts}/emr-logs/*"]
  }
  statement {
    sid = "Glue"
    actions = [
      "glue:GetDatabase", "glue:GetDatabases", "glue:GetTable", "glue:GetTables", "glue:CreateTable",
      "glue:UpdateTable", "glue:DeleteTable", "glue:GetPartitions", "glue:BatchGetPartition",
    ]
    resources = concat(
      ["arn:aws:glue:${var.region}:${local.account_id}:catalog"],
      [for d in local.glue_dbs : "arn:aws:glue:${var.region}:${local.account_id}:database/${d}"],
      [for d in local.glue_dbs : "arn:aws:glue:${var.region}:${local.account_id}:table/${d}/*"],
    )
  }
  statement {
    sid       = "LakeKey"
    actions   = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey"]
    resources = [local.in["security/kms_lake_arn"]]
  }
  statement {
    sid       = "LakeFormationData" # LF 가 켜진 위치(P1-12)의 임시 자격증명
    actions   = ["lakeformation:GetDataAccess"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "emr_job" {
  name   = "emr-job"
  role   = aws_iam_role.emr_job.id
  policy = data.aws_iam_policy_document.emr_job.json
}

locals {
  spark_defaults = jsondecode(file("${path.module}/../../../p1/batch/spark-defaults.json"))
  spark_conf = merge(local.spark_defaults, {
    "spark.sql.catalog.glue.warehouse" = "s3://${local.in["p1/lakehouse/silver_bucket"]}/"
    # OpenLineage Spark 리스너: MERGE·INSERT OVERWRITE 의 입력·출력 테이블(컬럼 계보 포함)을 Marquez 로 보낸다.
    # 리스너 실패는 잡을 실패시키지 않는다. jar 는 build_spark_jobs.sh 가 artifacts/spark/lib 에 올린다
    "spark.jars"                       = "${local.spark_defaults["spark.jars"]},s3://${local.artifacts}/spark/lib/openlineage-spark.jar"
    "spark.extraListeners"             = "io.openlineage.spark.agent.OpenLineageSparkListener"
    "spark.openlineage.transport.type" = "http"
    "spark.openlineage.transport.url"  = "http://marquez.${var.project}.internal:5000"
    "spark.openlineage.namespace"      = "${var.project}-${var.env}"
  })
  spark_prefix        = "s3://${local.artifacts}/spark/${var.spark_jobs_version}"
  spark_submit_params = join(" ", concat([for k, v in local.spark_conf : "--conf ${k}=${v}"], ["--py-files ${local.spark_prefix}/p1-jobs.zip"]))
}

module "emr_outputs" {
  source = "../../modules/ssm_outputs"
  prefix = "${local.ssm_prefix}/p1/processing"
  values = {
    emr_app_id          = aws_emrserverless_application.spark.id
    emr_job_role_arn    = aws_iam_role.emr_job.arn
    spark_entry_prefix  = local.spark_prefix
    spark_submit_params = local.spark_submit_params
    emr_log_uri         = "s3://${local.artifacts}/emr-logs/"
  }
}
