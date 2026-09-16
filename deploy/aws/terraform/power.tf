# ---------------------------------------------------------------------------
# The power switch
# ---------------------------------------------------------------------------
# `power = "off"` destroys everything that bills by the hour and keeps
# everything that holds state. On the current defaults (Fargate Spot,
# db.t4g.small) that is the difference between ~$93/month at the nightly
# sweeps' 8.5h/day, or ~$39 at 4h/day on weekdays, and ~$13/month for a
# deployment nobody is using yet. The ~$555 this header used to quote was
# on-demand at the original sizing, before spot.tf and the smaller RDS class.
#
#   terraform apply -var power=on  -var db_deletion_protection=false
#   terraform apply -var power=off -var db_deletion_protection=false
#   terraform apply -var power=on      # ~15 minutes to a serving stack
#
# POWERING OFF TAKES TWO APPLIES. RDS deletion protection is on by default and
# Terraform does not clear it on the way to deleting, so it has to come off in
# an earlier apply (an in-place change, no downtime). The precondition on
# aws_db_subnet_group fails the PLAN if you skip that step, which is the point:
# without it the apply reaches the RDS destroy and dies there, after the ALB,
# the NAT gateway and every ECS service are already gone.
#
# WHY DESTROY RATHER THAN STOP. The nightly sweeps already scale ECS to zero
# and stop RDS, and for a fifteen-and-a-half-hour window that is exactly
# right. It is
# still not an off switch:
#
#   * RDS force-starts a stopped instance after 7 days (data.tf). The nightly
#     cadence hides that — the sweep re-stops it the same night — but leave it
#     "stopped" for a month and it wakes up, bills a day of compute, and goes
#     back to sleep, repeatedly, with nothing reporting it.
#   * An ALB and a NAT gateway have no stopped state at all. They bill for as
#     long as they exist: $63/month between them, whether or not a single
#     request arrives.
#
# So "off" here means the resource is gone. What survives is listed below, and
# the split is deliberate: nothing that holds data is gated.
#
# KEPT WHEN OFF (state, or free, or painful to recreate):
#   S3 buckets          the corpus. Versioned; never gated.
#   EFS + access points the Qdrant index and Redis AOF. Bills per GB stored,
#                       so an empty filesystem is ~free — gating it would
#                       trade a real re-index against almost no saving.
#   Secrets Manager     see the trap below.
#   ECR                 the images power-on pulls. ~$0.50/month.
#   CloudWatch log      history outlives the compute that wrote it.
#     groups
#   SNS topic + email   an email subscription must be confirmed by clicking a
#     subscription      link. Gating it would mean re-confirming by hand on
#                       every power-on, and an unconfirmed topic is a silent
#                       alerting failure.
#   VPC, subnets, route VPC primitives are free. Keeping them also keeps the
#     tables, SGs, IGW,  CIDR layout and the security-group rules stable
#     S3 gateway endpt   across cycles.
#   ECS cluster         free, and an empty cluster costs nothing.
#   Cloud Map namespace ~$0.10/month; the per-service records are gated.
#   IAM roles           free. The policy statements that name gated
#                       resources degrade rather than the roles disappearing.
#
# THE TRAP THIS AVOIDS. Do NOT reach for a bare `terraform destroy` instead.
# `aws_secretsmanager_secret.app` carries `recovery_window_in_days = 30`
# (config.tf), which the APP_KEY rotation runbook depends on. Destroy
# schedules it for deletion, and the next apply fails with "a secret with
# this name is already scheduled for deletion" — for thirty days, unless you
# know to run `aws secretsmanager restore-secret`. Keeping the secret out of
# the power flag sidesteps that entirely. Two S3 buckets with versioning and
# no `force_destroy`, plus `deletion_protection` on RDS, would each stop a
# bare destroy as well — the RDS one also stops THIS teardown until it is
# cleared, which is why power-off is two applies rather than one.
#
# THE DATABASE. Powering off destroys the instance, which is what takes the
# $93/month compute and the $11.50/month storage to zero. The data is not
# lost: `skip_final_snapshot = false` means RDS writes `georag-pg-final` on
# the way out, and that snapshot is a MANUAL one — it is not governed by
# `db_backup_retention_days` and does not expire with the automated backups.
# Snapshot storage is billed, but free up to 100% of provisioned storage, so
# an empty database's snapshot is free.
#
# The snapshot NAME must vary if you power-cycle repeatedly: identifiers are
# unique per account, so a fixed name works once and the second power-off
# collides with the first one's snapshot. `db_final_snapshot_suffix` exists for
# that, and check-aws-power-flag.py fails if the name goes back to a constant.
#
# To come back up ON that data rather than on an empty database, pass the
# snapshot to the next apply:
#
#   terraform apply -var power=on -var restore_from_snapshot=georag-pg-final
#
# Leave `restore_from_snapshot` unset and power-on gives you a fresh, empty
# instance — which is the right default for the first ever apply, when no
# snapshot exists, and for a deployment whose stores are still empty.

variable "power" {
  description = <<-EOT
    "on" runs the platform. "off" destroys everything that bills hourly —
    ECS services, the ALB, the NAT gateway, RDS (final snapshot first), the
    nightly schedules and the alarms — and keeps S3, EFS, ECR, Secrets
    Manager, the log groups and the VPC. See the header of power.tf.
  EOT
  type        = string
  default     = "on"

  validation {
    condition     = contains(["on", "off"], var.power)
    error_message = "power must be \"on\" or \"off\"."
  }
}

variable "restore_from_snapshot" {
  description = <<-EOT
    RDS snapshot identifier to restore the database from on power-on, e.g.
    "georag-pg-final" (what powering off leaves behind). Unset means create a
    fresh, empty instance — correct for the first apply, when no snapshot
    exists yet.

    Changing this on a LIVE instance replaces it, which destroys the running
    database. Set it only on the apply that brings the platform back up.
  EOT
  type        = string
  default     = null
}

locals {
  # 1 when the platform should be running, 0 when it should not. Used as
  # `count` on hourly-billed resources and to empty the `for_each` maps.
  on = var.power == "on" ? 1 : 0

  # The database, or null when powered off. Everything that reads an
  # attribute off it goes through here so the reference degrades in one
  # place rather than at a dozen call sites.
  db = one(aws_db_instance.this)

  # The name the final snapshot is written under. Empty suffix keeps the
  # historical `<prefix>-pg-final`, which is what every doc and the
  # restore_from_snapshot examples already name; a suffix makes each teardown
  # in a repeating cadence land on its own identifier instead of colliding.
  db_final_snapshot = (
    var.db_final_snapshot_suffix == ""
    ? "${local.name}-pg-final"
    : "${local.name}-pg-final-${var.db_final_snapshot_suffix}"
  )
}
