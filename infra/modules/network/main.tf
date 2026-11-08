locals {
  az_index = { for i, az in var.azs : az => i }

  nat_count = var.nat_mode == "none" ? 0 : (var.nat_mode == "single" ? 1 : length(var.azs))

  # app 라우트 테이블(AZ별)이 바라볼 NAT. single 모드면 모두 첫 번째 NAT
  app_nat_index = { for az, i in local.az_index : az => (var.nat_mode == "per_az" ? i : 0) }

  # ---------------------------------------------------------------------------
  # NACL 규칙. NACL은 상태가 없으므로 응답 트래픽도 명시한다.
  # 임시 포트는 NAT·ELB·Lambda 가 1024-65535 를 쓰므로 그 범위를 연다.
  # 번호는 10 단위로 띄워 둔다(사이에 규칙을 끼워 넣을 수 있게).
  # ---------------------------------------------------------------------------
  public_rules = concat(
    [
      { n = 50, egress = false, action = "deny", proto = "tcp", from = 22, to = 22, cidr = "0.0.0.0/0" },
      { n = 60, egress = false, action = "deny", proto = "tcp", from = 3389, to = 3389, cidr = "0.0.0.0/0" },
      { n = 100, egress = false, action = "allow", proto = "-1", from = 0, to = 0, cidr = var.vpc_cidr },
      { n = 200, egress = false, action = "allow", proto = "tcp", from = 1024, to = 65535, cidr = "0.0.0.0/0" },
      { n = 100, egress = true, action = "allow", proto = "-1", from = 0, to = 0, cidr = var.vpc_cidr },
      { n = 200, egress = true, action = "allow", proto = "tcp", from = 443, to = 443, cidr = "0.0.0.0/0" },
    ],
    var.allow_http_egress ? [
      { n = 210, egress = true, action = "allow", proto = "tcp", from = 80, to = 80, cidr = "0.0.0.0/0" },
    ] : [],
  )

  app_rules = concat(
    [
      { n = 100, egress = false, action = "allow", proto = "-1", from = 0, to = 0, cidr = var.vpc_cidr },
      { n = 200, egress = false, action = "allow", proto = "tcp", from = 1024, to = 65535, cidr = "0.0.0.0/0" },
      { n = 100, egress = true, action = "allow", proto = "-1", from = 0, to = 0, cidr = var.vpc_cidr },
      { n = 200, egress = true, action = "allow", proto = "tcp", from = 443, to = 443, cidr = "0.0.0.0/0" },
    ],
    var.allow_http_egress ? [
      { n = 210, egress = true, action = "allow", proto = "tcp", from = 80, to = 80, cidr = "0.0.0.0/0" },
    ] : [],
  )

  data_rules = concat(
    [for i, p in var.data_service_ports :
      { n = 100 + 10 * i, egress = false, action = "allow", proto = "tcp", from = p, to = p, cidr = var.tier_cidrs.app }
    ],
    [
      { n = 200, egress = false, action = "allow", proto = "-1", from = 0, to = 0, cidr = var.tier_cidrs.data },
      { n = 100, egress = true, action = "allow", proto = "tcp", from = 1024, to = 65535, cidr = var.tier_cidrs.app },
      { n = 200, egress = true, action = "allow", proto = "-1", from = 0, to = 0, cidr = var.tier_cidrs.data },
    ],
  )

  nacl_rules = merge(
    { for r in local.public_rules : "public-${r.egress ? "out" : "in"}-${r.n}" => merge(r, { tier = "public" }) },
    { for r in local.app_rules : "app-${r.egress ? "out" : "in"}-${r.n}" => merge(r, { tier = "app" }) },
    { for r in local.data_rules : "data-${r.egress ? "out" : "in"}-${r.n}" => merge(r, { tier = "data" }) },
  )
}

# ---------------------------------------------------------------------------
# VPC, 인터넷 게이트웨이
# ---------------------------------------------------------------------------
resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true # 인터페이스 엔드포인트의 프라이빗 DNS에 필요

  tags = { Name = "${var.name}-vpc" }
}

# 기본 보안 그룹은 규칙을 모두 비워 아무것도 허용하지 않게 한다(CIS 권고)
resource "aws_default_security_group" "this" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = "${var.name}-default-deny" }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = "${var.name}-igw" }
}

# ---------------------------------------------------------------------------
# 서브넷: 공인 / 앱 / 데이터 × AZ
# ---------------------------------------------------------------------------
resource "aws_subnet" "public" {
  for_each                = local.az_index
  vpc_id                  = aws_vpc.this.id
  availability_zone       = each.key
  cidr_block              = var.public_subnets[each.value]
  map_public_ip_on_launch = false # 공인 서브넷이어도 자동 공인 IP는 주지 않는다

  tags = { Name = "${var.name}-public-${substr(each.key, -1, 1)}", tier = "public" }
}

resource "aws_subnet" "app" {
  for_each          = local.az_index
  vpc_id            = aws_vpc.this.id
  availability_zone = each.key
  cidr_block        = var.app_subnets[each.value]

  tags = { Name = "${var.name}-app-${substr(each.key, -1, 1)}", tier = "app" }
}

resource "aws_subnet" "data" {
  for_each          = local.az_index
  vpc_id            = aws_vpc.this.id
  availability_zone = each.key
  cidr_block        = var.data_subnets[each.value]

  tags = { Name = "${var.name}-data-${substr(each.key, -1, 1)}", tier = "data" }
}

# ---------------------------------------------------------------------------
# NAT 게이트웨이 (모드에 따라 0, 1, AZ 수만큼)
# ---------------------------------------------------------------------------
resource "aws_eip" "nat" {
  count  = local.nat_count
  domain = "vpc"
  tags   = { Name = "${var.name}-nat-eip-${count.index}" }
}

resource "aws_nat_gateway" "this" {
  count         = local.nat_count
  allocation_id = aws_eip.nat[count.index].id
  subnet_id     = aws_subnet.public[var.azs[count.index]].id
  tags          = { Name = "${var.name}-nat-${substr(var.azs[count.index], -1, 1)}" }

  depends_on = [aws_internet_gateway.this]
}

# ---------------------------------------------------------------------------
# 라우트 테이블
#   rt-public : 0.0.0.0/0 → IGW
#   rt-app-*  : 0.0.0.0/0 → NAT (AZ별 테이블. single 모드면 같은 NAT)
#   rt-data   : local 만. 기본 경로가 없어 데이터 계층은 인터넷으로 나갈 수 없다
# ---------------------------------------------------------------------------
resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = "${var.name}-rt-public" }
}

resource "aws_route" "public_default" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.this.id
}

resource "aws_route_table_association" "public" {
  for_each       = aws_subnet.public
  subnet_id      = each.value.id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table" "app" {
  for_each = local.az_index
  vpc_id   = aws_vpc.this.id
  tags     = { Name = "${var.name}-rt-app-${substr(each.key, -1, 1)}" }
}

resource "aws_route" "app_default" {
  for_each               = local.nat_count > 0 ? local.az_index : {}
  route_table_id         = aws_route_table.app[each.key].id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.this[local.app_nat_index[each.key]].id
}

resource "aws_route_table_association" "app" {
  for_each       = aws_subnet.app
  subnet_id      = each.value.id
  route_table_id = aws_route_table.app[each.key].id
}

resource "aws_route_table" "data" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = "${var.name}-rt-data" }
}

resource "aws_route_table_association" "data" {
  for_each       = aws_subnet.data
  subnet_id      = each.value.id
  route_table_id = aws_route_table.data.id
}

# ---------------------------------------------------------------------------
# NACL: 계층별 1개
# ---------------------------------------------------------------------------
resource "aws_network_acl" "public" {
  vpc_id     = aws_vpc.this.id
  subnet_ids = [for s in aws_subnet.public : s.id]
  tags       = { Name = "${var.name}-nacl-public" }
}

resource "aws_network_acl" "app" {
  vpc_id     = aws_vpc.this.id
  subnet_ids = [for s in aws_subnet.app : s.id]
  tags       = { Name = "${var.name}-nacl-app" }
}

resource "aws_network_acl" "data" {
  vpc_id     = aws_vpc.this.id
  subnet_ids = [for s in aws_subnet.data : s.id]
  tags       = { Name = "${var.name}-nacl-data" }
}

locals {
  nacl_ids = {
    public = aws_network_acl.public.id
    app    = aws_network_acl.app.id
    data   = aws_network_acl.data.id
  }
}

resource "aws_network_acl_rule" "this" {
  for_each       = local.nacl_rules
  network_acl_id = local.nacl_ids[each.value.tier]
  rule_number    = each.value.n
  egress         = each.value.egress
  rule_action    = each.value.action
  protocol       = each.value.proto
  from_port      = each.value.proto == "-1" ? null : each.value.from
  to_port        = each.value.proto == "-1" ? null : each.value.to
  cidr_block     = each.value.cidr
}

# ---------------------------------------------------------------------------
# VPC 엔드포인트
#   게이트웨이(S3, DynamoDB): 무료. 레이크 트래픽이 NAT 처리 요금을 타지 않게 한다
#   인터페이스: AZ당 시간 과금. 토글로 켠다
# ---------------------------------------------------------------------------
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [for rt in aws_route_table.app : rt.id]
  tags              = { Name = "${var.name}-vpce-s3" }
}

resource "aws_vpc_endpoint" "dynamodb" {
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${var.region}.dynamodb"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [for rt in aws_route_table.app : rt.id]
  tags              = { Name = "${var.name}-vpce-dynamodb" }
}

resource "aws_security_group" "endpoints" {
  name        = "${var.name}-sg-endpoints"
  description = "Interface VPC endpoints: HTTPS from inside the VPC"
  vpc_id      = aws_vpc.this.id
  tags        = { Name = "${var.name}-sg-endpoints" }
}

resource "aws_vpc_security_group_ingress_rule" "endpoints_https" {
  security_group_id = aws_security_group.endpoints.id
  description       = "HTTPS from VPC"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = var.vpc_cidr
}

resource "aws_vpc_endpoint" "interface" {
  for_each            = var.enable_interface_endpoints ? toset(var.interface_endpoint_services) : toset([])
  vpc_id              = aws_vpc.this.id
  service_name        = "com.amazonaws.${var.region}.${each.value}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [for s in aws_subnet.app : s.id]
  security_group_ids  = [aws_security_group.endpoints.id]
  private_dns_enabled = true
  tags                = { Name = "${var.name}-vpce-${replace(each.value, ".", "-")}" }
}

# ---------------------------------------------------------------------------
# VPC 흐름 로그 → S3 (Parquet, 하이브 파티션) — Athena로 REJECT 를 조회해 NACL을 디버깅한다
# ---------------------------------------------------------------------------
resource "aws_flow_log" "this" {
  count                    = var.flow_log_bucket_arn == null ? 0 : 1
  vpc_id                   = aws_vpc.this.id
  traffic_type             = "ALL"
  log_destination_type     = "s3"
  log_destination          = "${var.flow_log_bucket_arn}/vpc-flow-logs/"
  max_aggregation_interval = 600

  destination_options {
    file_format                = "parquet"
    hive_compatible_partitions = true
    per_hour_partition         = true
  }

  tags = { Name = "${var.name}-flow-logs" }
}
