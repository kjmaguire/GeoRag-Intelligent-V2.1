output "alb_dns_name" {
  description = "Point the application's DNS record at this."
  value       = aws_lb.this.dns_name
}

output "cluster_name" {
  value = aws_ecs_cluster.this.name
}

output "ecr_repository_urls" {
  description = "Push targets for the CD workflow."
  value       = { for k, v in aws_ecr_repository.this : k => v.repository_url }
}

output "db_endpoint" {
  value = aws_db_instance.this.address
}

output "db_master_secret_arn" {
  description = "RDS-managed master password. Nothing in this repo holds it."
  value       = aws_db_instance.this.master_user_secret[0].secret_arn
}

output "app_secret_arn" {
  description = "Set the application secrets here, out of band. config.tf lists the expected keys."
  value       = aws_secretsmanager_secret.app.arn
}

output "alerts_topic_arn" {
  value = aws_sns_topic.alerts.arn
}

output "maintenance_window_hours" {
  description = <<-EOT
    Derived from the two cron expressions, not configured. Surfaced so an
    operator can confirm the alert suppression window matches what they
    think the schedule is — the check the Azure parity script did for the
    cron and the DST guard.
  EOT
  value       = local.maintenance_window_hours
}

output "private_subnet_ids" {
  description = "For the CD workflow's AWS_PRIVATE_SUBNET_IDS secret (comma-separated)."
  value       = join(",", aws_subnet.private[*].id)
}

output "task_security_group_id" {
  description = "For the CD workflow's AWS_TASK_SECURITY_GROUP_ID secret."
  value       = aws_security_group.tasks.id
}
