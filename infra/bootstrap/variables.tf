variable "project" {
  type    = string
  default = "danbi"
}

variable "env" {
  type    = string
  default = "dev"
}

variable "region" {
  type    = string
  default = "ap-northeast-2"
}

variable "owner" {
  type    = string
  default = "jeonghun"
}

variable "github_repo" {
  description = "OIDC로 배포를 허용할 GitHub 저장소 (owner/name)"
  type        = string
}

variable "alert_email" {
  description = "예산 알림을 받을 이메일"
  type        = string
}

variable "monthly_budget_usd" {
  description = "월 예산 상한. 실습 계정이라면 작게 잡는다"
  type        = number
  default     = 150
}

variable "allowed_regions" {
  description = "apply 역할이 리소스를 만들 수 있는 리전"
  type        = list(string)
  default     = ["ap-northeast-2"]
}
