output "public_url" {
  description = <<-EOT
    The address to open in a browser. THE ONLY PLACE IT IS WRITTEN DOWN when
    `edge = "cloudfront"`: the hostname is the distribution's own
    *.cloudfront.net name, which does not exist until apply, is not in any
    tfvars file, and changes if the deployment is powered off and back on.
    With `edge = "alb"` it is just https://app_domain.

    Null when `power = "off"`, because then there is nothing serving.
  EOT
  value       = local.public_host == "" ? null : local.public_url
}

output "alb_dns_name" {
  description = <<-EOT
    The load balancer's own hostname.

    NOT the public address in the default edge mode: with `edge =
    "cloudfront"` the load balancer listens on plain HTTP and its security
    group admits only CloudFront's edges, so reaching this directly times out
    (and is meant to). Use `public_url`.

    With `edge = "alb"` this is the public edge, and `manage_dns = true`
    aliases app_domain at it so nothing needs pointing by hand. It stays
    useful for reaching the stack before DNS propagates, and it is what you
    paste at an external registrar when manage_dns is false.
  EOT
  value       = try(one(aws_lb.this).dns_name, null)
}

output "cluster_name" {
  value = aws_ecs_cluster.this.name
}

output "ecr_repository_urls" {
  description = "Push targets for the CD workflow."
  value       = { for k, v in aws_ecr_repository.this : k => v.repository_url }
}

output "db_endpoint" {
  value = try(local.db.address, null)
}

output "db_master_secret_arn" {
  description = "RDS-managed master password. Nothing in this repo holds it."
  value       = try(local.db.master_user_secret[0].secret_arn, null)
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

    FRACTIONAL since 2026-09-16: the default schedule is 17:00 to 08:30
    local, so this reads 15.5. If it ever reads a whole number of hours when
    the crons are not both on the hour, scheduler.tf has gone back to
    truncating and the dead-air suppressor is short by the remainder.
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

output "app_key_rotation_task_family" {
  description = <<-EOT
    The task definition deploy/aws/rotation/rotate-app-key.sh runs the
    re-encryption in. Not startable outside a rotation: it references an
    APP_KEY_NEXT secret key that only exists while one is in flight.
  EOT
  value       = try(one(aws_ecs_task_definition.app_key_rotation).family, null)
}
