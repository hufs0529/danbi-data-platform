# P1-04 CDC: Debezium on MSK Connect
#   커넥터 설정 원본은 p1/cdc/connectors/*.tftpl 이고, CI 의 p1/cdc/lint.py 가 같은 템플릿을 렌더링해 검사한다.

variable "plugin_key" {
  description = "artifacts 버킷 안의 커스텀 플러그인 zip 키. 비어 있으면 커넥터를 만들지 않는다(build_plugin.sh --upload 후 설정)"
  type        = string
  default     = ""
}

variable "kafkaconnect_version" {
  type    = string
  default = "3.7.x"
}

variable "connectors_enabled" {
  description = "false 면 커넥터만 지운다. 오프셋은 connect.offsets.cdc 에 남아 다시 만들면 이어받는다(F-01 훈련)"
  type        = bool
  default     = true
}

data "aws_ssm_parameter" "sources" {
  for_each = toset(["mysql_endpoint", "pg_endpoint", "db_user_secret_prefix", "db_clients_sg_id"])
  name     = "${local.ssm_prefix}/p1/sources/${each.key}"
}

locals {
  src           = { for k, p in data.aws_ssm_parameter.sources : k => p.value }
  cdc_enabled   = var.msk_enabled && var.plugin_key != "" && var.connectors_enabled
  bootstrap     = var.msk_enabled ? aws_msk_cluster.this[0].bootstrap_brokers_sasl_iam : ""
  connector_dir = "${path.module}/../../../p1/cdc/connectors"
  connector_vars = {
    mysql_host          = local.src["mysql_endpoint"]
    pg_host             = local.src["pg_endpoint"]
    secret_prefix       = local.src["db_user_secret_prefix"]
    bootstrap           = local.bootstrap
    schema_registry_url = "http://schema-registry.${var.project}.internal:8081"
    region              = var.region
  }
  connectors = {
    "mysql-commerce" = "MySQL commerce → cdc.mysql.commerce.*"
    "pg-payment"     = "PostgreSQL payment → cdc.pg.public.*, outbox → events.payment"
  }
}

# ---------------------------------------------------------------------------
# 아티팩트 버킷: 커넥터 플러그인, Flink 앱 zip, Spark 잡, MWAA DAG (P1 공용)
# ---------------------------------------------------------------------------
module "artifacts" {
  source        = "../../modules/secure_bucket"
  name          = "${local.name}-artifacts-${local.account_id}"
  kms_key_arn   = local.in["security/kms_lake_arn"]
  versioning    = true # MWAA 가 DAG·requirements 버전을 요구한다
  force_destroy = true
}

resource "aws_cloudwatch_log_group" "connect" {
  name              = "/${var.project}/${var.env}/msk-connect"
  retention_in_days = 30
  kms_key_id        = local.in["security/kms_logs_arn"]
}

resource "aws_mskconnect_custom_plugin" "cdc" {
  count        = var.plugin_key == "" ? 0 : 1
  name         = "${local.name}-cdc-${replace(basename(var.plugin_key), ".zip", "")}"
  content_type = "ZIP"
  location {
    s3 {
      bucket_arn = module.artifacts.arn
      file_key   = var.plugin_key
    }
  }
}

resource "aws_mskconnect_worker_configuration" "cdc" {
  name = "${local.name}-cdc-worker-v1" # 내용이 바뀌면 이름(버전)을 올린다. 워커 설정은 수정이 아니라 새 리비전
  properties_file_content = templatefile("${local.connector_dir}/worker.properties.tftpl", {
    schema_registry_url = local.connector_vars.schema_registry_url
    region              = var.region
  })
}

data "aws_iam_policy_document" "connect_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["kafkaconnect.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "connect" {
  name               = "${local.name}-msk-connect"
  assume_role_policy = data.aws_iam_policy_document.connect_trust.json
}

data "aws_iam_policy_document" "connect" {
  statement {
    sid       = "Cluster"
    actions   = ["kafka-cluster:Connect", "kafka-cluster:DescribeCluster", "kafka-cluster:WriteDataIdempotently"]
    resources = ["${local.msk_arn_prefix}:cluster/${local.cluster_name}/*"]
  }
  statement {
    sid = "Topics"
    actions = [
      "kafka-cluster:DescribeTopic", "kafka-cluster:ReadData", "kafka-cluster:WriteData",
      "kafka-cluster:CreateTopic", "kafka-cluster:DescribeTopicDynamicConfiguration",
    ]
    # CDC·이벤트·하트비트·스키마 이력·오프셋 토픽과 MSK Connect 내부 토픽만
    resources = [for t in ["cdc.*", "events.*", "__debezium-heartbeat.*", "schemahistory.*", "connect.offsets.*", "__amazon_msk_connect_*"] :
    "${local.msk_arn_prefix}:topic/${local.cluster_name}/*/${t}"]
  }
  statement {
    sid       = "Groups"
    actions   = ["kafka-cluster:AlterGroup", "kafka-cluster:DescribeGroup"]
    resources = ["${local.msk_arn_prefix}:group/${local.cluster_name}/*/*"]
  }
  statement {
    sid     = "DebeziumSecrets"
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      "arn:aws:secretsmanager:${var.region}:${local.account_id}:secret:${local.src["db_user_secret_prefix"]}mysql/debezium-*",
      "arn:aws:secretsmanager:${var.region}:${local.account_id}:secret:${local.src["db_user_secret_prefix"]}postgres/debezium-*",
    ]
  }
}

resource "aws_iam_role_policy" "connect" {
  name   = "cdc"
  role   = aws_iam_role.connect.id
  policy = data.aws_iam_policy_document.connect.json
}

resource "aws_mskconnect_connector" "cdc" {
  for_each             = local.cdc_enabled ? local.connectors : {}
  name                 = "${local.name}-${each.key}"
  description          = each.value
  kafkaconnect_version = var.kafkaconnect_version

  connector_configuration = jsondecode(templatefile("${local.connector_dir}/${each.key}.json.tftpl", local.connector_vars))

  capacity {
    provisioned_capacity {
      mcu_count    = 1
      worker_count = 1 # tasks.max=1: binlog·WAL 은 순서대로 하나의 스트림이라 병렬화 이득이 없다
    }
  }

  kafka_cluster {
    apache_kafka_cluster {
      bootstrap_servers = local.bootstrap
      vpc {
        # DB 에도 붙어야 하므로 두 신분증을 모두 단다
        security_groups = [local.src["db_clients_sg_id"], local.msk_clients_sg]
        subnets         = local.app_subnet_ids
      }
    }
  }

  kafka_cluster_client_authentication {
    authentication_type = "IAM"
  }

  kafka_cluster_encryption_in_transit {
    encryption_type = "TLS"
  }

  plugin {
    custom_plugin {
      arn      = aws_mskconnect_custom_plugin.cdc[0].arn
      revision = aws_mskconnect_custom_plugin.cdc[0].latest_revision
    }
  }

  worker_configuration {
    arn      = aws_mskconnect_worker_configuration.cdc.arn
    revision = aws_mskconnect_worker_configuration.cdc.latest_revision
  }

  log_delivery {
    worker_log_delivery {
      cloudwatch_logs {
        enabled   = true
        log_group = aws_cloudwatch_log_group.connect.name
      }
    }
  }

  service_execution_role_arn = aws_iam_role.connect.arn
}

module "cdc_outputs" {
  source = "../../modules/ssm_outputs"
  prefix = "${local.ssm_prefix}/p1/streaming"
  values = {
    artifacts_bucket = module.artifacts.id
    connector_names  = join(",", [for c in aws_mskconnect_connector.cdc : c.name])
    connect_role_arn = aws_iam_role.connect.arn
  }
}
