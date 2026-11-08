# P1 이벤트 버스: MSK Provisioned(IAM 인증) + Confluent Schema Registry(ECS)
# 토픽은 Terraform 이 아니라 p1/streaming/topics.yaml(토픽 as code)로 관리한다.

variable "msk_enabled" {
  description = "false 면 클러스터를 지운다(make pause). 토픽 데이터도 사라지므로 Bronze 가 원본이다"
  type        = bool
  default     = true
}

variable "kafka_version" {
  description = "3.9.x 는 MSK 권장 버전이고 KRaft·ZooKeeper 를 모두 지원하는 마지막 라인. 배포 전 get-compatible-kafka-versions 로 확인"
  type        = string
  default     = "3.9.x.kraft"
}

variable "broker_instance_type" {
  type    = string
  default = "kafka.t3.small"
}

variable "broker_volume_gb" {
  type    = number
  default = 50
}

variable "schema_registry_image_tag" {
  type    = string
  default = "7.9.2-iam"
}

data "aws_ssm_parameter" "in" {
  for_each = toset([
    "network/vpc_id", "network/data_subnet_ids", "network/app_subnet_ids",
    "security/kms_lake_arn", "security/kms_logs_arn",
    "platform/ecs_cluster_arn", "platform/ecs_execution_role_arn", "platform/ecs_log_group",
    "platform/ecr_registry", "platform/ecr_prefix", "platform/namespace_id",
    "p1/sources/msk_clients_sg_id",
  ])
  name = "${local.ssm_prefix}/${each.key}"
}

locals {
  in              = { for k, p in data.aws_ssm_parameter.in : k => p.value }
  data_subnet_ids = split(",", local.in["network/data_subnet_ids"])
  app_subnet_ids  = split(",", local.in["network/app_subnet_ids"])
  msk_clients_sg  = local.in["p1/sources/msk_clients_sg_id"]
  cluster_name    = "${local.name}-msk"
  # 클러스터 UUID 를 모르는 단계에서도 정책을 쓸 수 있도록 이름 기반 ARN 패턴
  msk_arn_prefix = "arn:aws:kafka:${var.region}:${local.account_id}"
}

# ---------------------------------------------------------------------------
# 보안 그룹: 브로커는 msk-clients 신분증을 단 워크로드만 9098(IAM)로 받는다
# ---------------------------------------------------------------------------
resource "aws_security_group" "msk" {
  name        = "${local.name}-sg-msk"
  description = "MSK brokers (IAM auth 9098)"
  vpc_id      = local.in["network/vpc_id"]
}

resource "aws_vpc_security_group_ingress_rule" "msk_from_clients" {
  security_group_id            = aws_security_group.msk.id
  referenced_security_group_id = local.msk_clients_sg
  ip_protocol                  = "tcp"
  from_port                    = 9098
  to_port                      = 9098
  description                  = "msk-clients to brokers (SASL_SSL IAM)"
}

resource "aws_vpc_security_group_ingress_rule" "msk_self" {
  security_group_id            = aws_security_group.msk.id
  referenced_security_group_id = aws_security_group.msk.id
  ip_protocol                  = "-1"
  description                  = "broker to broker replication"
}

# ---------------------------------------------------------------------------
# MSK
# ---------------------------------------------------------------------------
resource "aws_msk_configuration" "this" {
  name           = "${local.name}-msk-config"
  kafka_versions = [trimsuffix(var.kafka_version, ".kraft")]
  server_properties = join("\n", [
    "auto.create.topics.enable=false", # 토픽은 topics.yaml 로만 만든다(오타 토픽 방지)
    "default.replication.factor=2",
    # 브로커 2대라 min.insync.replicas=2 로 두면 롤링 패치 동안 acks=all 쓰기가 멈춘다.
    # 1 로 두는 대가: 리더만 받은 쓰기는 그 브로커 디스크가 죽으면 사라질 수 있다. 운영은 3대 + 2 (ADR AWS-04)
    "min.insync.replicas=1",
    "unclean.leader.election.enable=false",
    "num.partitions=3",
    "log.retention.hours=48",
    "message.max.bytes=2097152",
  ])
}

resource "aws_cloudwatch_log_group" "msk" {
  name              = "/${var.project}/${var.env}/msk"
  retention_in_days = 14
  kms_key_id        = local.in["security/kms_logs_arn"]
}

resource "aws_msk_cluster" "this" {
  count                  = var.msk_enabled ? 1 : 0
  cluster_name           = local.cluster_name
  kafka_version          = var.kafka_version
  number_of_broker_nodes = 2

  broker_node_group_info {
    instance_type   = var.broker_instance_type
    client_subnets  = local.data_subnet_ids
    security_groups = [aws_security_group.msk.id]
    storage_info {
      ebs_storage_info {
        volume_size = var.broker_volume_gb
      }
    }
  }

  configuration_info {
    arn      = aws_msk_configuration.this.arn
    revision = aws_msk_configuration.this.latest_revision
  }

  client_authentication {
    unauthenticated = false
    sasl {
      iam = true
    }
  }

  encryption_info {
    encryption_at_rest_kms_key_arn = local.in["security/kms_lake_arn"]
    encryption_in_transit {
      client_broker = "TLS"
      in_cluster    = true
    }
  }

  # DEFAULT 수준에서도 컨슈머 그룹 지연(EstimatedMaxTimeLag, MaxOffsetLag)이 나온다.
  # 건수가 아니라 '몇 초 밀렸나'로 알림을 걸기 위해 EstimatedMaxTimeLag 를 쓴다
  enhanced_monitoring = "DEFAULT"

  logging_info {
    broker_logs {
      cloudwatch_logs {
        enabled   = true
        log_group = aws_cloudwatch_log_group.msk.name
      }
    }
  }
}

# ---------------------------------------------------------------------------
# Schema Registry (Confluent, ECS Fargate 1대)
#   Flink SQL 의 debezium-avro-confluent 포맷과 Debezium AvroConverter 가 Confluent 와이어 포맷을 쓰기 때문.
#   저장소는 MSK 의 _schemas 토픽. REST API 는 VPC 안 + SG(msk-clients)로만 막는다(운영은 인증 추가)
# ---------------------------------------------------------------------------
resource "aws_security_group" "schema_registry" {
  name        = "${local.name}-sg-schema-registry"
  description = "Schema Registry REST 8081"
  vpc_id      = local.in["network/vpc_id"]
}

resource "aws_vpc_security_group_ingress_rule" "sr_from_clients" {
  security_group_id            = aws_security_group.schema_registry.id
  referenced_security_group_id = local.msk_clients_sg
  ip_protocol                  = "tcp"
  from_port                    = 8081
  to_port                      = 8081
  description                  = "msk-clients to schema registry"
}

resource "aws_vpc_security_group_egress_rule" "sr_all" {
  security_group_id = aws_security_group.schema_registry.id
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

data "aws_iam_policy_document" "schema_registry" {
  statement {
    actions   = ["kafka-cluster:Connect", "kafka-cluster:DescribeCluster"]
    resources = ["${local.msk_arn_prefix}:cluster/${local.cluster_name}/*"]
  }
  statement {
    actions = [
      "kafka-cluster:CreateTopic", "kafka-cluster:DescribeTopic", "kafka-cluster:DescribeTopicDynamicConfiguration",
      "kafka-cluster:ReadData", "kafka-cluster:WriteData",
    ]
    resources = ["${local.msk_arn_prefix}:topic/${local.cluster_name}/*/_schemas"]
  }
  statement {
    actions   = ["kafka-cluster:AlterGroup", "kafka-cluster:DescribeGroup"]
    resources = ["${local.msk_arn_prefix}:group/${local.cluster_name}/*/schema-registry*"]
  }
}

module "schema_registry" {
  source             = "../../modules/ecs_service"
  name               = "${local.name}-schema-registry"
  cluster_arn        = local.in["platform/ecs_cluster_arn"]
  image              = "${local.in["platform/ecr_registry"]}/${local.in["platform/ecr_prefix"]}/schema-registry:${var.schema_registry_image_tag}"
  cpu                = 512
  memory             = 1024
  container_port     = 8081
  execution_role_arn = local.in["platform/ecs_execution_role_arn"]
  log_group_name     = local.in["platform/ecs_log_group"]
  task_policy_json   = data.aws_iam_policy_document.schema_registry.json
  environment = {
    SCHEMA_REGISTRY_HOST_NAME                                     = "schema-registry.${var.project}.internal"
    SCHEMA_REGISTRY_LISTENERS                                     = "http://0.0.0.0:8081"
    SCHEMA_REGISTRY_KAFKASTORE_BOOTSTRAP_SERVERS                  = var.msk_enabled ? aws_msk_cluster.this[0].bootstrap_brokers_sasl_iam : "disabled:9098"
    SCHEMA_REGISTRY_KAFKASTORE_SECURITY_PROTOCOL                  = "SASL_SSL"
    SCHEMA_REGISTRY_KAFKASTORE_SASL_MECHANISM                     = "AWS_MSK_IAM"
    SCHEMA_REGISTRY_KAFKASTORE_SASL_JAAS_CONFIG                   = "software.amazon.msk.auth.iam.IAMLoginModule required;"
    SCHEMA_REGISTRY_KAFKASTORE_SASL_CLIENT_CALLBACK_HANDLER_CLASS = "software.amazon.msk.auth.iam.IAMClientCallbackHandler"
    SCHEMA_REGISTRY_KAFKASTORE_TOPIC                              = "_schemas"
    SCHEMA_REGISTRY_KAFKASTORE_TOPIC_REPLICATION_FACTOR           = "2"
    SCHEMA_REGISTRY_SCHEMA_COMPATIBILITY_LEVEL                    = "backward"
    SCHEMA_REGISTRY_LEADER_ELIGIBILITY                            = "true"
  }
  health_check_command           = ["CMD-SHELL", "curl -sf http://localhost:8081/subjects || exit 1"]
  create_service                 = true
  desired_count                  = var.msk_enabled ? 1 : 0
  subnet_ids                     = local.app_subnet_ids
  security_group_ids             = [aws_security_group.schema_registry.id, local.msk_clients_sg]
  service_discovery_namespace_id = local.in["platform/namespace_id"]
  discovery_name                 = "schema-registry"
}

module "outputs" {
  source = "../../modules/ssm_outputs"
  prefix = "${local.ssm_prefix}/p1/streaming"
  values = {
    cluster_name          = local.cluster_name
    cluster_arn           = var.msk_enabled ? aws_msk_cluster.this[0].arn : "disabled"
    bootstrap_brokers_iam = var.msk_enabled ? aws_msk_cluster.this[0].bootstrap_brokers_sasl_iam : "disabled"
    msk_sg_id             = aws_security_group.msk.id
    schema_registry_url   = "http://schema-registry.${var.project}.internal:8081"
    schema_registry_sg_id = aws_security_group.schema_registry.id
  }
}

# ---------------------------------------------------------------------------
# 운영 도구 태스크(RunTask): 토픽 as code, Schema Registry 설정·등록.
# 이미지는 생성기 이미지(p1 도구 포함)를 재사용한다. 클러스터 관리 권한은 이 역할에만 있다
# ---------------------------------------------------------------------------
variable "tools_image_tag" {
  type    = string
  default = "0.1.0"
}

data "aws_iam_policy_document" "stream_admin" {
  statement {
    actions   = ["kafka-cluster:Connect", "kafka-cluster:DescribeCluster"]
    resources = ["${local.msk_arn_prefix}:cluster/${local.cluster_name}/*"]
  }
  statement {
    actions = [
      "kafka-cluster:CreateTopic", "kafka-cluster:AlterTopic", "kafka-cluster:DescribeTopic",
      "kafka-cluster:DescribeTopicDynamicConfiguration", "kafka-cluster:AlterTopicDynamicConfiguration",
    ]
    resources = ["${local.msk_arn_prefix}:topic/${local.cluster_name}/*/*"]
  }
  statement {
    actions   = ["ssm:GetParameter"]
    resources = ["arn:aws:ssm:${var.region}:${local.account_id}:parameter${local.ssm_prefix}/p1/streaming/*"]
  }
}

module "stream_admin" {
  source             = "../../modules/ecs_service"
  name               = "${local.name}-stream-admin"
  cluster_arn        = local.in["platform/ecs_cluster_arn"]
  image              = "${local.in["platform/ecr_registry"]}/${local.in["platform/ecr_prefix"]}/generator:${var.tools_image_tag}"
  cpu                = 256
  memory             = 512
  command            = ["p1.streaming.topics", "plan"]
  execution_role_arn = local.in["platform/ecs_execution_role_arn"]
  log_group_name     = local.in["platform/ecs_log_group"]
  task_policy_json   = data.aws_iam_policy_document.stream_admin.json
  environment = {
    MSK_BOOTSTRAP_PARAM = "${local.ssm_prefix}/p1/streaming/bootstrap_brokers_iam"
    SCHEMA_REGISTRY_URL = "http://schema-registry.${var.project}.internal:8081"
  }
  create_service = false
}

module "admin_outputs" {
  source = "../../modules/ssm_outputs"
  prefix = "${local.ssm_prefix}/p1/streaming"
  values = {
    admin_task_family = module.stream_admin.task_definition_family
  }
}
