output "vpc_id" {
  value = module.network.vpc_id
}

output "app_subnet_ids" {
  value = module.network.app_subnet_ids
}

output "data_subnet_ids" {
  value = module.network.data_subnet_ids
}

output "nacl_ids" {
  value = module.network.nacl_ids
}

output "nat_gateway_ids" {
  value = module.network.nat_gateway_ids
}
