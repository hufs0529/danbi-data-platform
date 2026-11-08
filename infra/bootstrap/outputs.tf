output "state_bucket" {
  value = aws_s3_bucket.tfstate.bucket
}

output "state_kms_key_arn" {
  value = aws_kms_key.tfstate.arn
}

output "plan_role_arn" {
  description = "GitHub 변수 AWS_PLAN_ROLE_ARN 에 넣는다"
  value       = aws_iam_role.plan.arn
}

output "apply_role_arn" {
  description = "GitHub environment dev 의 변수 AWS_APPLY_ROLE_ARN 에 넣는다"
  value       = aws_iam_role.apply.arn
}

output "backend_hcl" {
  description = "infra/backend.hcl 에 그대로 붙여 넣는다"
  value       = <<-EOT
    bucket       = "${aws_s3_bucket.tfstate.bucket}"
    region       = "${var.region}"
    encrypt      = true
    kms_key_id   = "${aws_kms_key.tfstate.arn}"
    use_lockfile = true
  EOT
}
