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

variable "kms_key_arn" {
  description = "null 이면 SSE-S3(AES256). 레이크·감사 버킷은 고객 관리형 KMS 키를 넘긴다"
  type        = string
  default     = null
}

variable "versioning" {
  type    = bool
  default = true
}

variable "force_destroy" {
  description = "dev 에서 destroy 를 쉽게 하려면 true. 감사 버킷은 false"
  type        = bool
  default     = false
}

variable "prefix_expirations" {
  description = "접두사별 만료 일수 (예: { \"landing/\" = 30 }). 빈 문자열 키는 버킷 전체"
  type        = map(number)
  default     = {}
}

variable "noncurrent_expiration_days" {
  type    = number
  default = 30
}

variable "policy_documents" {
  description = "기본 정책(TLS 강제)에 합칠 추가 정책 JSON 목록"
  type        = list(string)
  default     = []
}

variable "object_lock" {
  description = "객체 잠금(WORM). 감사 로그용. 버킷 생성 시에만 켤 수 있다"
  type = object({
    mode = string # GOVERNANCE | COMPLIANCE
    days = number
  })
  default = null
}

variable "eventbridge" {
  description = "S3 이벤트를 EventBridge 로 보낼지"
  type        = bool
  default     = false
}

resource "aws_s3_bucket" "this" {
  bucket              = var.name
  force_destroy       = var.force_destroy
  object_lock_enabled = var.object_lock != null
}

resource "aws_s3_bucket_ownership_controls" "this" {
  bucket = aws_s3_bucket.this.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "this" {
  bucket                  = aws_s3_bucket.this.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "this" {
  bucket = aws_s3_bucket.this.id
  versioning_configuration {
    status = var.versioning || var.object_lock != null ? "Enabled" : "Suspended"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "this" {
  bucket = aws_s3_bucket.this.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = var.kms_key_arn == null ? "AES256" : "aws:kms"
      kms_master_key_id = var.kms_key_arn
    }
    bucket_key_enabled = var.kms_key_arn != null
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "this" {
  bucket = aws_s3_bucket.this.id

  rule {
    id     = "noncurrent-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = var.noncurrent_expiration_days
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  dynamic "rule" {
    for_each = var.prefix_expirations
    content {
      id     = "expire-${rule.key == "" ? "all" : trim(replace(rule.key, "/", "-"), "-")}"
      status = "Enabled"
      filter {
        prefix = rule.key
      }
      expiration {
        days = rule.value
      }
    }
  }
}

resource "aws_s3_bucket_object_lock_configuration" "this" {
  count  = var.object_lock == null ? 0 : 1
  bucket = aws_s3_bucket.this.id
  rule {
    default_retention {
      mode = var.object_lock.mode
      days = var.object_lock.days
    }
  }
  depends_on = [aws_s3_bucket_versioning.this]
}

data "aws_iam_policy_document" "this" {
  source_policy_documents = var.policy_documents

  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    resources = [aws_s3_bucket.this.arn, "${aws_s3_bucket.this.arn}/*"]
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "this" {
  bucket     = aws_s3_bucket.this.id
  policy     = data.aws_iam_policy_document.this.json
  depends_on = [aws_s3_bucket_public_access_block.this]
}

resource "aws_s3_bucket_notification" "this" {
  count       = var.eventbridge ? 1 : 0
  bucket      = aws_s3_bucket.this.id
  eventbridge = true
}

output "id" {
  value = aws_s3_bucket.this.id
}

output "arn" {
  value = aws_s3_bucket.this.arn
}
