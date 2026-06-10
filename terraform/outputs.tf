output "landing_bucket" {
  value       = aws_s3_bucket.landing.bucket
  description = "Prod landing bucket — set this as writer.landing_bucket in config/feeds.yaml."
}

output "rollup_cluster" {
  value = aws_ecs_cluster.main.name
}

output "rollup_task_family" {
  value = aws_ecs_task_definition.rollup.family
}

output "rollup_subnet_ids" {
  value       = data.aws_subnets.default.ids
  description = "Default-VPC subnets for the run-task network config."
}

output "rollup_security_group" {
  value = aws_security_group.rollup.id
}

output "hot_scratch_bucket" {
  value = aws_s3_bucket.hot_scratch.bucket
}
