# 스택 사이의 계약: 다른 스택이 읽을 값을 SSM Parameter Store 에 쓴다.
# terraform_remote_state 대신 이걸 쓰면 소비하는 쪽이 생산하는 쪽의 state 구조를 몰라도 된다.
terraform {
  required_version = ">= 1.10.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.0, < 7.0"
    }
  }
}

variable "prefix" {
  description = "예: /danbi/dev/network"
  type        = string
}

variable "values" {
  description = "이름 → 값. 리스트는 쉼표로 이어 붙여 넘긴다"
  type        = map(string)
}

resource "aws_ssm_parameter" "this" {
  for_each = var.values
  name     = "${var.prefix}/${each.key}"
  type     = "String"
  value    = each.value
}

output "names" {
  value = { for k, p in aws_ssm_parameter.this : k => p.name }
}
