terraform {
  required_version = ">= 1.10.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.0, < 7.0"
    }
  }
  backend "s3" {}
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      project    = var.project
      env        = var.env
      owner      = var.owner
      managed_by = "terraform"
      stack      = "10-network"
    }
  }
}
