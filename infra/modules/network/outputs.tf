output "vpc_id" {
  value = aws_vpc.this.id
}

output "vpc_cidr" {
  value = aws_vpc.this.cidr_block
}

output "public_subnet_ids" {
  value = [for az in var.azs : aws_subnet.public[az].id]
}

output "app_subnet_ids" {
  value = [for az in var.azs : aws_subnet.app[az].id]
}

output "data_subnet_ids" {
  value = [for az in var.azs : aws_subnet.data[az].id]
}

output "app_route_table_ids" {
  value = [for az in var.azs : aws_route_table.app[az].id]
}

output "data_route_table_id" {
  value = aws_route_table.data.id
}

output "nacl_ids" {
  value = local.nacl_ids
}

output "endpoints_security_group_id" {
  value = aws_security_group.endpoints.id
}

output "s3_gateway_endpoint_id" {
  value = aws_vpc_endpoint.s3.id
}

output "s3_prefix_list_id" {
  description = "보안 그룹 이그레스를 S3로만 제한할 때 쓴다"
  value       = aws_vpc_endpoint.s3.prefix_list_id
}

output "nat_gateway_ids" {
  value = aws_nat_gateway.this[*].id
}
