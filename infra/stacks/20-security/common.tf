variable "project" {
  type = string
}

variable "env" {
  type = string
}

variable "region" {
  type = string
}

variable "owner" {
  type = string
}

data "aws_caller_identity" "current" {}

locals {
  name       = "${var.project}-${var.env}"
  ssm_prefix = "/${var.project}/${var.env}"
  account_id = data.aws_caller_identity.current.account_id
}
