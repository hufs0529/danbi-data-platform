terraform {
  required_version = ">= 1.10.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.0, < 7.0"
    }
  }

  # 부트스트랩은 state 버킷을 만드는 스택이라 로컬 state를 쓴다.
  # 적용 후 terraform.tfstate는 안전한 곳에 보관하거나 `terraform init -migrate-state`로 S3에 옮긴다.
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      project    = var.project
      env        = var.env
      owner      = var.owner
      managed_by = "terraform"
      stack      = "bootstrap"
    }
  }
}
