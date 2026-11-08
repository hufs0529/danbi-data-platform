# 네트워크 설계 규칙을 테스트로 고정한다. 자격증명 없이 mock provider 로 plan 한다.
#   terraform test   (또는 tofu test)  — infra/modules/network 에서 실행

mock_provider "aws" {}

variables {
  name       = "t"
  region     = "ap-northeast-2"
  vpc_cidr   = "10.20.0.0/16"
  azs        = ["ap-northeast-2a", "ap-northeast-2c"]
  tier_cidrs = { public = "10.20.0.0/19", app = "10.20.32.0/19", data = "10.20.64.0/19" }

  public_subnets = ["10.20.0.0/20", "10.20.16.0/20"]
  app_subnets    = ["10.20.32.0/20", "10.20.48.0/20"]
  data_subnets   = ["10.20.64.0/20", "10.20.80.0/20"]
}

run "single_nat_routes_both_app_tables" {
  command = plan

  variables {
    nat_mode = "single"
  }

  assert {
    condition     = length(aws_nat_gateway.this) == 1
    error_message = "single 모드는 NAT 1개"
  }
  assert {
    condition     = length(aws_route.app_default) == 2
    error_message = "두 AZ의 앱 라우트 테이블 모두 기본 경로가 있어야 한다"
  }
}

run "per_az_nat" {
  command = plan

  variables {
    nat_mode = "per_az"
  }

  assert {
    condition     = length(aws_nat_gateway.this) == 2
    error_message = "per_az 모드는 AZ마다 NAT"
  }
}

run "paused_has_no_egress" {
  command = plan

  variables {
    nat_mode = "none"
  }

  assert {
    condition     = length(aws_nat_gateway.this) == 0 && length(aws_route.app_default) == 0
    error_message = "none 모드는 NAT도 기본 경로도 없어야 한다"
  }
}

run "data_tier_is_isolated" {
  command = plan

  assert {
    condition = alltrue([
      for k, r in aws_network_acl_rule.this : r.cidr_block != "0.0.0.0/0" if startswith(k, "data-")
    ])
    error_message = "데이터 NACL에는 0.0.0.0/0 규칙이 없어야 한다"
  }

  assert {
    condition = alltrue([
      for k, r in aws_network_acl_rule.this : r.cidr_block == "10.20.32.0/19" if startswith(k, "data-in-1")
    ])
    error_message = "데이터 계층 서비스 포트는 앱 계층에서만 열린다"
  }
}

run "public_blocks_remote_admin_ports" {
  command = plan

  assert {
    condition = (
      aws_network_acl_rule.this["public-in-50"].rule_action == "deny" &&
      aws_network_acl_rule.this["public-in-50"].from_port == 22 &&
      aws_network_acl_rule.this["public-in-60"].from_port == 3389
    )
    error_message = "공인 NACL은 22, 3389 를 명시적으로 거부한다"
  }
}

run "interface_endpoints_toggle" {
  command = plan

  variables {
    enable_interface_endpoints = true
  }

  assert {
    condition     = length(aws_vpc_endpoint.interface) == 12
    error_message = "인터페이스 엔드포인트 12종"
  }
}
