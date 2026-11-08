# Fargate 태스크 정의 + (선택) 서비스. 서비스 없이 태스크 정의만 만들면 RunTask(배치·Airflow·Step Functions)용이다.
terraform {
  required_version = ">= 1.10.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.0, < 7.0"
    }
  }
}

variable "name" {
  type = string
}

variable "cluster_arn" {
  type = string
}

variable "image" {
  type = string
}

variable "cpu" {
  type    = number
  default = 512
}

variable "memory" {
  type    = number
  default = 1024
}

variable "cpu_architecture" {
  description = "ARM64(Graviton) 가 x86 보다 저렴하다. 이미지도 arm64 로 빌드한다"
  type        = string
  default     = "ARM64"
}

variable "command" {
  type    = list(string)
  default = null
}

variable "container_port" {
  type    = number
  default = null
}

variable "environment" {
  type    = map(string)
  default = {}
}

variable "secrets" {
  description = "환경변수 이름 → Secrets Manager/SSM ARN (실행 역할이 읽는다)"
  type        = map(string)
  default     = {}
}

variable "execution_role_arn" {
  type = string
}

variable "task_policy_json" {
  description = "태스크 역할(애플리케이션 권한) 정책 JSON"
  type        = string
  default     = null
}

variable "log_group_name" {
  type = string
}

variable "create_service" {
  type    = bool
  default = true
}

variable "desired_count" {
  type    = number
  default = 1
}

variable "subnet_ids" {
  type    = list(string)
  default = []
}

variable "security_group_ids" {
  type    = list(string)
  default = []
}

variable "capacity_provider" {
  type    = string
  default = "FARGATE"
}

variable "service_discovery_namespace_id" {
  description = "Cloud Map 네임스페이스. 주면 <name>.<namespace> 로 등록한다"
  type        = string
  default     = null
}

variable "discovery_name" {
  type    = string
  default = null
}

variable "target_group_arn" {
  type    = string
  default = null
}

variable "health_check_command" {
  type    = list(string)
  default = null
}

data "aws_region" "current" {}

data "aws_iam_policy_document" "trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "task" {
  name               = "${var.name}-task"
  assume_role_policy = data.aws_iam_policy_document.trust.json
}

resource "aws_iam_role_policy" "task" {
  count  = var.task_policy_json == null ? 0 : 1
  name   = "app"
  role   = aws_iam_role.task.id
  policy = var.task_policy_json
}

# ECS Exec(디버깅 셸)에 필요
data "aws_iam_policy_document" "exec" {
  statement {
    actions = [
      "ssmmessages:CreateControlChannel", "ssmmessages:CreateDataChannel",
      "ssmmessages:OpenControlChannel", "ssmmessages:OpenDataChannel",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "exec" {
  name   = "ecs-exec"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.exec.json
}

locals {
  container = merge(
    {
      name      = "app"
      image     = var.image
      essential = true
      environment = [
        for k, v in var.environment : { name = k, value = v }
      ]
      secrets = [
        for k, v in var.secrets : { name = k, valueFrom = v }
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = var.log_group_name
          awslogs-region        = data.aws_region.current.region
          awslogs-stream-prefix = var.name
        }
      }
      readonlyRootFilesystem = false
    },
    var.command == null ? {} : { command = var.command },
    var.container_port == null ? {} : {
      portMappings = [{ containerPort = var.container_port, protocol = "tcp" }]
    },
    var.health_check_command == null ? {} : {
      healthCheck = { command = var.health_check_command, interval = 30, timeout = 5, retries = 3, startPeriod = 60 }
    },
  )
}

resource "aws_ecs_task_definition" "this" {
  family                   = var.name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.cpu
  memory                   = var.memory
  execution_role_arn       = var.execution_role_arn
  task_role_arn            = aws_iam_role.task.arn
  container_definitions    = jsonencode([local.container])

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = var.cpu_architecture
  }
}

resource "aws_service_discovery_service" "this" {
  count = var.create_service && var.service_discovery_namespace_id != null ? 1 : 0
  name  = coalesce(var.discovery_name, var.name)
  dns_config {
    namespace_id   = var.service_discovery_namespace_id
    routing_policy = "MULTIVALUE"
    dns_records {
      type = "A"
      ttl  = 10
    }
  }
}

resource "aws_ecs_service" "this" {
  count                  = var.create_service ? 1 : 0
  name                   = var.name
  cluster                = var.cluster_arn
  task_definition        = aws_ecs_task_definition.this.arn
  desired_count          = var.desired_count
  enable_execute_command = true

  capacity_provider_strategy {
    capacity_provider = var.capacity_provider
    weight            = 1
  }

  network_configuration {
    subnets          = var.subnet_ids
    security_groups  = var.security_group_ids
    assign_public_ip = false
  }

  dynamic "service_registries" {
    for_each = aws_service_discovery_service.this
    content {
      registry_arn = service_registries.value.arn
    }
  }

  dynamic "load_balancer" {
    for_each = var.target_group_arn == null ? [] : [var.target_group_arn]
    content {
      target_group_arn = load_balancer.value
      container_name   = "app"
      container_port   = var.container_port
    }
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
}

output "task_definition_arn" {
  value = aws_ecs_task_definition.this.arn
}

output "task_definition_family" {
  value = aws_ecs_task_definition.this.family
}

output "task_role_arn" {
  value = aws_iam_role.task.arn
}

output "task_role_name" {
  value = aws_iam_role.task.name
}

output "service_name" {
  value = var.create_service ? aws_ecs_service.this[0].name : null
}
