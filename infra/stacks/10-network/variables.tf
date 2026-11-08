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

variable "azs" {
  type    = list(string)
  default = ["ap-northeast-2a", "ap-northeast-2c"]
}

variable "nat_mode" {
  description = "none | single | per_az"
  type        = string
  default     = "single"
}

variable "enable_interface_endpoints" {
  type    = bool
  default = false
}
