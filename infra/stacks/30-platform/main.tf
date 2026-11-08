variable "enable_ops_instance" {
  description = "SSM 포트 포워딩용 운영 EC2(t4g.nano). RDS·사내 앱 접속에 쓴다"
  type        = bool
  default     = true
}

variable "ecr_repositories" {
  type = list(string)
  default = [
    "schema-registry", "generator", "dbt-runner", "recon",
    "kl-worker", "kl-api", "hwp-converter", "kl-reconciler", "corpus-seeder",
  ]
}

data "aws_ssm_parameter" "vpc_id" {
  name = "${local.ssm_prefix}/network/vpc_id"
}

data "aws_ssm_parameter" "app_subnet_ids" {
  name = "${local.ssm_prefix}/network/app_subnet_ids"
}

data "aws_ssm_parameter" "kms_logs_arn" {
  name = "${local.ssm_prefix}/security/kms_logs_arn"
}

locals {
  vpc_id         = data.aws_ssm_parameter.vpc_id.value
  app_subnet_ids = split(",", data.aws_ssm_parameter.app_subnet_ids.value)
}

# ---------------------------------------------------------------------------
# ECS 클러스터 (Fargate + Fargate Spot). 배치성 태스크(dbt, 대조, 시더)는 Spot 으로 돌린다
# ---------------------------------------------------------------------------
resource "aws_ecs_cluster" "this" {
  name = local.name
  setting {
    name  = "containerInsights"
    value = "disabled" # 비용. 장애 훈련 기간에만 enabled 로 켠다
  }
}

resource "aws_ecs_cluster_capacity_providers" "this" {
  cluster_name       = aws_ecs_cluster.this.name
  capacity_providers = ["FARGATE", "FARGATE_SPOT"]
  default_capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
  }
}

# 모든 ECS 태스크가 공유하는 실행 역할: 이미지 pull, 로그, 이 프로젝트 비밀 읽기
data "aws_iam_policy_document" "ecs_tasks_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ecs_execution" {
  name               = "${local.name}-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_trust.json
}

resource "aws_iam_role_policy_attachment" "ecs_execution" {
  role       = aws_iam_role.ecs_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "ecs_execution_secrets" {
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = ["arn:aws:secretsmanager:${var.region}:${local.account_id}:secret:${local.name}/*"]
  }
  statement {
    actions   = ["ssm:GetParameters"]
    resources = ["arn:aws:ssm:${var.region}:${local.account_id}:parameter${local.ssm_prefix}/*"]
  }
  statement {
    actions   = ["kms:Decrypt"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["secretsmanager.${var.region}.amazonaws.com", "ssm.${var.region}.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "ecs_execution_secrets" {
  name   = "project-secrets"
  role   = aws_iam_role.ecs_execution.id
  policy = data.aws_iam_policy_document.ecs_execution_secrets.json
}

resource "aws_cloudwatch_log_group" "ecs" {
  name              = "/${var.project}/${var.env}/ecs"
  retention_in_days = 30
  kms_key_id        = data.aws_ssm_parameter.kms_logs_arn.value
}

# ---------------------------------------------------------------------------
# ECR: 태그 불변 + 푸시 시 스캔 + 최근 10개만 보관
# ---------------------------------------------------------------------------
resource "aws_ecr_repository" "this" {
  for_each             = toset(var.ecr_repositories)
  name                 = "${local.name}/${each.value}"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = true
  image_scanning_configuration {
    scan_on_push = true
  }
  encryption_configuration {
    encryption_type = "KMS"
  }
}

resource "aws_ecr_lifecycle_policy" "this" {
  for_each   = aws_ecr_repository.this
  repository = each.value.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "keep last 10 images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 10 }
      action       = { type = "expire" }
    }]
  })
}

# ---------------------------------------------------------------------------
# 서비스 디스커버리: schema-registry.danbi.internal 같은 내부 이름
# ---------------------------------------------------------------------------
resource "aws_service_discovery_private_dns_namespace" "this" {
  name = "${var.project}.internal"
  vpc  = local.vpc_id
}

# ---------------------------------------------------------------------------
# 운영 접속용 EC2: SSH 키·인바운드 규칙 없음. SSM 포트 포워딩의 경유지로만 쓴다
# ---------------------------------------------------------------------------
resource "aws_security_group" "ops" {
  name        = "${local.name}-sg-ops"
  description = "SSM-only ops host. No inbound."
  vpc_id      = local.vpc_id
}

resource "aws_vpc_security_group_egress_rule" "ops_all" {
  security_group_id = aws_security_group.ops.id
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

data "aws_ssm_parameter" "al2023_arm64" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

data "aws_iam_policy_document" "ec2_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ops" {
  name               = "${local.name}-ops-host"
  assume_role_policy = data.aws_iam_policy_document.ec2_trust.json
}

resource "aws_iam_role_policy_attachment" "ops_ssm" {
  role       = aws_iam_role.ops.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "ops" {
  name = "${local.name}-ops-host"
  role = aws_iam_role.ops.name
}

resource "aws_instance" "ops" {
  count                       = var.enable_ops_instance ? 1 : 0
  ami                         = data.aws_ssm_parameter.al2023_arm64.value
  instance_type               = "t4g.nano"
  subnet_id                   = local.app_subnet_ids[0]
  vpc_security_group_ids      = [aws_security_group.ops.id]
  iam_instance_profile        = aws_iam_instance_profile.ops.name
  associate_public_ip_address = false

  metadata_options {
    http_tokens = "required" # IMDSv2 강제
  }

  root_block_device {
    encrypted   = true
    volume_size = 8
  }

  user_data = <<-EOT
    #!/bin/bash
    dnf install -y postgresql16 mariadb105 jq
  EOT

  tags = { Name = "${local.name}-ops" }

  lifecycle {
    ignore_changes = [ami]
  }
}

module "outputs" {
  source = "../../modules/ssm_outputs"
  prefix = "${local.ssm_prefix}/platform"
  values = {
    ecs_cluster_arn        = aws_ecs_cluster.this.arn
    ecs_cluster_name       = aws_ecs_cluster.this.name
    ecs_execution_role_arn = aws_iam_role.ecs_execution.arn
    ecs_log_group          = aws_cloudwatch_log_group.ecs.name
    ecr_registry           = split("/", aws_ecr_repository.this["kl-api"].repository_url)[0]
    ecr_prefix             = local.name
    namespace_id           = aws_service_discovery_private_dns_namespace.this.id
    namespace_name         = aws_service_discovery_private_dns_namespace.this.name
    ops_sg_id              = aws_security_group.ops.id
    ops_instance_id        = var.enable_ops_instance ? aws_instance.ops[0].id : "none"
  }
}

output "ecr_repository_urls" {
  value = { for k, r in aws_ecr_repository.this : k => r.repository_url }
}

output "ops_instance_id" {
  value = var.enable_ops_instance ? aws_instance.ops[0].id : null
}
