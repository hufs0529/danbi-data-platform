variable "name" {
  description = "리소스 이름 접두사 (예: danbi-dev)"
  type        = string
}

variable "region" {
  type = string
}

variable "vpc_cidr" {
  type    = string
  default = "10.20.0.0/16"
}

variable "azs" {
  description = "사용할 AZ. 서브넷 목록과 순서가 같아야 한다"
  type        = list(string)
}

variable "tier_cidrs" {
  description = "계층별 통짜 범위. NACL 규칙이 계층 단위 CIDR 하나로 쓰인다"
  type = object({
    public = string
    app    = string
    data   = string
  })
}

variable "public_subnets" {
  type = list(string)
}

variable "app_subnets" {
  type = list(string)
}

variable "data_subnets" {
  type = list(string)
}

variable "nat_mode" {
  description = "none: 인터넷 아웃바운드 없음, single: NAT 1개(비용 절감), per_az: AZ마다 NAT(가용성)"
  type        = string
  default     = "single"
  validation {
    condition     = contains(["none", "single", "per_az"], var.nat_mode)
    error_message = "nat_mode 는 none, single, per_az 중 하나다."
  }
}

variable "data_service_ports" {
  description = "앱 계층 → 데이터 계층으로 허용할 서비스 포트(규칙 번호 100부터 10씩)"
  type        = list(number)
  default     = [3306, 5432, 9098, 443]
}

variable "allow_http_egress" {
  description = "앱·공인 계층에서 인터넷 TCP 80 아웃바운드 허용(패키지 저장소 등)"
  type        = bool
  default     = true
}

variable "enable_interface_endpoints" {
  description = "인터페이스 엔드포인트(AZ당 시간 과금)를 만들지"
  type        = bool
  default     = false
}

variable "interface_endpoint_services" {
  type = list(string)
  default = [
    "sts", "secretsmanager", "kms", "logs", "monitoring",
    "ecr.api", "ecr.dkr", "ssm", "ssmmessages", "ec2messages", "sqs", "bedrock-runtime",
  ]
}

variable "flow_log_bucket_arn" {
  description = "VPC 흐름 로그를 받을 S3 버킷 ARN. null이면 흐름 로그를 만들지 않는다"
  type        = string
  default     = null
}
