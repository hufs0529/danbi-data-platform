data "aws_caller_identity" "current" {}

locals {
  name       = "${var.project}-${var.env}"
  ssm_prefix = "/${var.project}/${var.env}"
  account_id = data.aws_caller_identity.current.account_id

  # 10.20.0.0/16 = 공인 /19 + 앱 /19 + 데이터 /19 + 예비
  tier_cidrs = {
    public = "10.20.0.0/19"
    app    = "10.20.32.0/19"
    data   = "10.20.64.0/19"
  }
}

# ---------------------------------------------------------------------------
# 흐름 로그 버킷 (네트워크 스택이 가장 먼저 적용되므로 여기서 만든다)
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "flow_logs_delivery" {
  statement {
    sid     = "AWSLogDeliveryWrite"
    actions = ["s3:PutObject"]
    principals {
      type        = "Service"
      identifiers = ["delivery.logs.amazonaws.com"]
    }
    # 하이브 호환 파티션 경로(AWSLogs/aws-account-id=.../)까지 포함하도록 AWSLogs/* 로 연다
    resources = ["arn:aws:s3:::${local.name}-netlogs-${local.account_id}/vpc-flow-logs/AWSLogs/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
    condition {
      test     = "StringEquals"
      variable = "s3:x-amz-acl"
      values   = ["bucket-owner-full-control"]
    }
  }
  statement {
    sid     = "AWSLogDeliveryAclCheck"
    actions = ["s3:GetBucketAcl"]
    principals {
      type        = "Service"
      identifiers = ["delivery.logs.amazonaws.com"]
    }
    resources = ["arn:aws:s3:::${local.name}-netlogs-${local.account_id}"]
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

module "flow_log_bucket" {
  source             = "../../modules/secure_bucket"
  name               = "${local.name}-netlogs-${local.account_id}"
  versioning         = false
  force_destroy      = true
  prefix_expirations = { "vpc-flow-logs/" = 30 }
  policy_documents   = [data.aws_iam_policy_document.flow_logs_delivery.json]
}

# ---------------------------------------------------------------------------
# VPC: 3계층 × 2 AZ
# ---------------------------------------------------------------------------
module "network" {
  source = "../../modules/network"

  name       = local.name
  region     = var.region
  vpc_cidr   = "10.20.0.0/16"
  azs        = var.azs
  tier_cidrs = local.tier_cidrs

  public_subnets = ["10.20.0.0/20", "10.20.16.0/20"]
  app_subnets    = ["10.20.32.0/20", "10.20.48.0/20"]
  data_subnets   = ["10.20.64.0/20", "10.20.80.0/20"]

  nat_mode                   = var.nat_mode
  enable_interface_endpoints = var.enable_interface_endpoints
  data_service_ports         = [3306, 5432, 9098, 443]
  flow_log_bucket_arn        = module.flow_log_bucket.arn
}

module "outputs" {
  source = "../../modules/ssm_outputs"
  prefix = "${local.ssm_prefix}/network"
  values = {
    vpc_id                = module.network.vpc_id
    vpc_cidr              = module.network.vpc_cidr
    public_subnet_ids     = join(",", module.network.public_subnet_ids)
    app_subnet_ids        = join(",", module.network.app_subnet_ids)
    data_subnet_ids       = join(",", module.network.data_subnet_ids)
    app_route_table_ids   = join(",", module.network.app_route_table_ids)
    app_tier_cidr         = local.tier_cidrs.app
    data_tier_cidr        = local.tier_cidrs.data
    endpoints_sg_id       = module.network.endpoints_security_group_id
    s3_prefix_list_id     = module.network.s3_prefix_list_id
    flow_logs_bucket_name = module.flow_log_bucket.id
  }
}
