# P1 레이크하우스 저장소: S3 버킷(계층별) + Glue 데이터베이스 + Athena 워크그룹
# 버킷을 계층별로 나누는 이유: 버킷 정책·KMS·CloudTrail 데이터 이벤트·Lake Formation 등록 범위를 계층 단위로 다르게 둔다.

variable "athena_scan_limit_bytes" {
  description = "워크그룹별 쿼리당 스캔 상한(실수로 전체 스캔하는 비용 사고 방지)"
  type        = map(number)
  default = {
    engineering = 107374182400 # 100GB
    pipeline    = 214748364800 # 200GB
    analyst     = 10737418240  # 10GB
    finance     = 10737418240
  }
}

data "aws_ssm_parameter" "kms_lake_arn" {
  name = "${local.ssm_prefix}/security/kms_lake_arn"
}

locals {
  kms_lake = data.aws_ssm_parameter.kms_lake_arn.value
  # 20-security 의 CloudTrail 데이터 이벤트가 이 이름(silver, gold)을 기준으로 잡혀 있다
  buckets = {
    landing = { versioning = true, expirations = { "pg_settlement/" = 30, "quarantine/" = 90 }, eventbridge = true }
    bronze  = { versioning = true, expirations = {}, eventbridge = false }
    silver  = { versioning = true, expirations = {}, eventbridge = false }
    gold    = { versioning = true, expirations = {}, eventbridge = false }
    athena  = { versioning = false, expirations = { "" = 7 }, eventbridge = false }
  }
}

module "bucket" {
  source   = "../../modules/secure_bucket"
  for_each = local.buckets

  name               = "${local.name}-${each.key}-${local.account_id}"
  kms_key_arn        = local.kms_lake
  versioning         = each.value.versioning
  force_destroy      = true # dev. 운영은 false
  prefix_expirations = each.value.expirations
  # Iceberg 버킷: 이전 버전은 짧게(실수 삭제 복구용). 현재 버전 만료는 절대 걸지 않는다(테이블 파손)
  noncurrent_expiration_days = 7
  eventbridge                = each.value.eventbridge
}

# ---------------------------------------------------------------------------
# Glue 데이터베이스(= Athena 스키마)
# ---------------------------------------------------------------------------
locals {
  databases = {
    bronze     = "s3://${module.bucket["bronze"].id}/"
    silver     = "s3://${module.bucket["silver"].id}/"
    ops        = "s3://${module.bucket["silver"].id}/_ops/"
    gold       = "s3://${module.bucket["gold"].id}/"
    gold_audit = "s3://${module.bucket["gold"].id}/_audit/" # WAP: 검증 전 결과. 분석가 권한 없음
    rt         = "s3://${module.bucket["gold"].id}/_rt/"    # 실시간 집계(속보치)
  }
}

resource "aws_glue_catalog_database" "this" {
  for_each     = local.databases
  name         = each.key
  location_uri = each.value
  description  = "${local.name} ${each.key}"
}

# ---------------------------------------------------------------------------
# Athena 워크그룹: 사람·파이프라인을 나눠 결과 위치·스캔 상한·비용 귀속을 다르게 둔다.
# enforce_workgroup_configuration=true 라 사용자가 결과 위치를 바꿔 다른 버킷으로 빼낼 수 없다.
# ---------------------------------------------------------------------------
resource "aws_athena_workgroup" "this" {
  for_each      = var.athena_scan_limit_bytes
  name          = "${local.name}-${each.key}"
  force_destroy = true
  configuration {
    enforce_workgroup_configuration    = true
    publish_cloudwatch_metrics_enabled = true
    bytes_scanned_cutoff_per_query     = each.value
    engine_version {
      selected_engine_version = "Athena engine version 3"
    }
    result_configuration {
      output_location = "s3://${module.bucket["athena"].id}/${each.key}/"
      encryption_configuration {
        encryption_option = "SSE_KMS"
        kms_key_arn       = local.kms_lake
      }
    }
  }
  tags = { cost_center = each.key }
}

module "outputs" {
  source = "../../modules/ssm_outputs"
  prefix = "${local.ssm_prefix}/p1/lakehouse"
  values = merge(
    { for k, b in module.bucket : "${k}_bucket" => b.id },
    { for k, b in module.bucket : "${k}_bucket_arn" => b.arn },
    { for k, w in aws_athena_workgroup.this : "workgroup_${k}" => w.name },
    { databases = join(",", keys(local.databases)) },
  )
}
