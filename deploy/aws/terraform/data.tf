# Durable stores: RDS, EFS, S3 (ADR-0022).
#
# Two of the three fix a defect the Azure deployment carried. That is
# deliberate: a fresh deployment is the one moment when not replicating a
# known problem costs nothing.

# ---------------------------------------------------------------------------
# PostgreSQL 18 + PostGIS
# ---------------------------------------------------------------------------
# Managed Postgres is viable because of the extension audit in ADR-0022:
# of the fourteen extensions docker/postgresql/init/*.sql creates, the only
# two RDS lacks (pg_ivm, pg_stat_kcache) have zero call sites in this
# repository, and auto_explain is a shared_preload_libraries parameter with
# no reader either. h3 and h3_postgis — which sat outside Azure Flexible
# Server's azure.extensions allow-list and were a known problem there — are
# supported on RDS for PG 18.

resource "aws_db_subnet_group" "this" {
  name       = local.name
  subnet_ids = aws_subnet.private[*].id

  # Fails the PLAN when a power-off is requested with deletion protection
  # still on, instead of letting the apply get as far as the RDS destroy and
  # fail there — by which point the ALB, NAT gateway and every ECS service
  # are already gone and the stack is half torn down.
  #
  # It also fires when there is no database to protect (a power-off of a
  # deployment that never had one). That is a harmless false alarm with an
  # obvious remedy, and the trade is worth it: the real case it catches is
  # the one that leaves a mess.
  lifecycle {
    precondition {
      condition     = var.power == "on" || var.db_deletion_protection == false
      error_message = "power=off cannot destroy the database while db_deletion_protection is true. Clear it in a separate, earlier apply:\n  terraform apply -var power=on  -var db_deletion_protection=false\n  terraform apply -var power=off -var db_deletion_protection=false\nSee variables.tf for why this takes two applies."
    }
  }
}

resource "aws_db_parameter_group" "this" {
  name   = "${local.name}-pg18"
  family = "postgres18"

  # The extensions RDS gates behind shared_preload_libraries. auto_explain
  # is here rather than in an init script because on RDS it is a parameter,
  # not a CREATE EXTENSION — the init scripts' `CREATE EXTENSION
  # auto_explain` has no RDS equivalent and no reader in this repository
  # either way.
  #
  # pg_cron, not pg_partman_bgw: found live, on a real first apply into an
  # empty account (2026-09-16) — ModifyDBParameterGroup rejected
  # pg_partman_bgw outright. "Invalid parameter value ... allowed values
  # are: auto_explain,orafce,pgaudit,pglogical,pg_bigm,pg_cron,
  # pg_hint_plan,pg_overexplain,pg_prewarm,pg_similarity,pg_stat_monitor,
  # pg_stat_statements,pg_tle,pg_transport,plprofiler" — RDS does not run
  # third-party background workers at all, not "supports fewer than
  # compose does". pg_partman itself (CREATE EXTENSION pg_partman,
  # deploy/aws/bootstrap.sql) still works fine as a plain extension; only
  # its own scheduler is unavailable. cron.database_name below plus
  # bootstrap.sql's `SELECT cron.schedule(...)` call is AWS's own
  # documented substitute: pg_cron drives partman.run_maintenance_proc()
  # instead of pg_partman_bgw doing it itself. Without this, the three
  # partman.create_parent() tables (audit.audit_ledger, workflow.workflow_runs,
  # usage.usage_events) would silently stop growing new partitions months
  # after go-live — not a boot-time failure, an inserts-fail-later one.
  parameter {
    name         = "shared_preload_libraries"
    value        = "pg_stat_statements,pg_cron,auto_explain"
    apply_method = "pending-reboot"
  }

  # pg_cron on RDS runs its scheduler against ONE database per instance;
  # this tells it which one. CREATE EXTENSION pg_cron and cron.schedule(...)
  # both have to run inside that same database (georag, matching
  # aws_db_instance.this.db_name below and bootstrap.sql's -d georag), not
  # in the RDS convention of installing it into the postgres maintenance DB.
  parameter {
    name         = "cron.database_name"
    value        = "georag"
    apply_method = "pending-reboot"
  }

  # Carried across because the Azure server did NOT have it and it cost
  # visibility: `log_min_duration_statement` was unset on Flexible Server,
  # so PostgreSQLLogs carried startup and checkpoint chatter and no slow
  # queries at all, while a CPU alarm fired at 03:00 with no query-level
  # evidence behind it (Ch 12 §1.2).
  parameter {
    name  = "log_min_duration_statement"
    value = "1000"
  }

  parameter {
    name  = "auto_explain.log_min_duration"
    value = "3000"
  }

  parameter {
    name  = "auto_explain.log_analyze"
    value = "0"
  }
}

resource "aws_db_instance" "this" {
  # Gated. Powering off destroys the instance after writing
  # `final_snapshot_identifier`; power.tf explains why "stopped" is not an
  # off switch (RDS force-starts after 7 days) and how to restore.
  count = local.on

  identifier     = "${local.name}-pg"
  engine         = "postgres"
  engine_version = var.db_engine_version
  instance_class = var.db_instance_class

  # Unset on a first apply and on an empty redeploy, which creates a fresh
  # instance. Set to `georag-pg-final` to come back up on the data the last
  # power-off preserved. `snapshot_identifier` is ignored once the instance
  # exists, so it cannot silently replace a live database on a re-apply —
  # but CHANGING it does force replacement, hence the warning on the
  # variable.
  snapshot_identifier = var.restore_from_snapshot

  allocated_storage     = var.db_allocated_storage_gb
  max_allocated_storage = var.db_allocated_storage_gb * 4
  storage_type          = "gp3"
  storage_encrypted     = true

  db_name = "georag"
  # `georag`, not something new. Ch 02 §1.3 records it as the initdb
  # bootstrap user that owns the georag database and every schema, and it is
  # what migrations connect as (config/database.php's MIGRATE_DB_USERNAME
  # default). Naming the RDS master anything else would mean every schema
  # ends up owned by a role the rest of the system has never heard of.
  username = "georag"
  # Generated and stored by AWS in Secrets Manager; nothing in this repo
  # or in CI ever holds it. The Azure equivalent lived in hand-set
  # container app env vars that drifted from .env.production.example.
  manage_master_user_password = true

  db_subnet_group_name   = aws_db_subnet_group.this.name
  vpc_security_group_ids = [aws_security_group.database.id]
  parameter_group_name   = aws_db_parameter_group.this.name
  publicly_accessible    = false

  backup_retention_period = var.db_backup_retention_days
  copy_tags_to_snapshot   = true

  # BOTH of these were constants until 2026-09-16, and both blocked the very
  # teardown power.tf is built around. Neither had ever run: there are no AWS
  # credentials in CI, so `terraform plan` has never executed in either power
  # state and the first power-off would have discovered them.
  #
  #   deletion_protection = true  -> `-var power=off` fails on the destroy
  #     with "Cannot delete protected DB Instance". Terraform does not clear
  #     the flag on its way to deleting, and count = 0 does not apply an
  #     attribute change to a resource that is going away. Power-off is
  #     therefore TWO applies; see the variable, and the plan-time
  #     precondition on aws_db_subnet_group that catches a skipped first one.
  #
  #   final_snapshot_identifier = "<fixed>"  -> works ONCE. RDS snapshot
  #     identifiers are unique per account, so the second power-off collides
  #     with the snapshot the first one wrote. Invisible until the second
  #     cycle, which is precisely when a recurring demo schedule meets it.
  deletion_protection       = var.db_deletion_protection
  skip_final_snapshot       = false
  final_snapshot_identifier = local.db_final_snapshot

  # Single-AZ, matching what Azure ran and what the nightly stop/start
  # implies: a Multi-AZ instance cannot be stopped. Making this Multi-AZ
  # means giving up the nightly shutdown, which is the largest cost lever
  # this deployment has.
  multi_az = false

  auto_minor_version_upgrade   = true
  performance_insights_enabled = true

  # A stopped instance is the intended state for seven hours a night, and
  # RDS force-starts one after 7 days. The nightly cadence makes that a
  # non-issue in practice; it is in the runbook so it is not a surprise.
  lifecycle {
    ignore_changes = [engine_version]
  }
}

# ---------------------------------------------------------------------------
# EFS — Qdrant and Redis
# ---------------------------------------------------------------------------
# Qdrant DID have persistent storage on Azure (an Azure Files share), so
# this is not a missing-volume fix. It removes that share's two real
# defects: it was mounted with the storage account key, so rotating the key
# broke the mount on the next restart, and it had a fixed quota whose
# exhaustion stalled the optimiser and generated 10.8M storage transactions
# in a day (2026-08-17..20). EFS is elastic and IAM-authorised.

resource "aws_efs_file_system" "this" {
  creation_token = local.name
  encrypted      = true

  # Elastic throughput rather than a provisioned figure: Qdrant's load is
  # bursty (an optimiser pass, then nothing) and the platform is off
  # overnight, which is the shape elastic bills well for.
  throughput_mode = "elastic"

  lifecycle_policy {
    transition_to_ia = "AFTER_30_DAYS"
  }

  tags = { Name = local.name }
}

resource "aws_efs_mount_target" "this" {
  count           = var.az_count
  file_system_id  = aws_efs_file_system.this.id
  subnet_id       = aws_subnet.private[count.index].id
  security_groups = [aws_security_group.database.id]
}

resource "aws_efs_access_point" "qdrant" {
  file_system_id = aws_efs_file_system.this.id

  posix_user {
    uid = 1000
    gid = 1000
  }

  root_directory {
    path = "/qdrant"
    creation_info {
      owner_uid   = 1000
      owner_gid   = 1000
      permissions = "0755"
    }
  }

  tags = { Name = "${local.name}-qdrant" }
}

# Redis had AOF OFF and no volume on Azure, so every restart — and every
# nightly scale-to-zero — dropped all sessions and any queued Horizon job.
# It is the one store in the platform that genuinely had no persistence.
# The volume is half the fix; the AOF setting is in the task definition.
resource "aws_efs_access_point" "redis" {
  file_system_id = aws_efs_file_system.this.id

  posix_user {
    uid = 999
    gid = 999
  }

  root_directory {
    path = "/redis"
    creation_info {
      owner_uid   = 999
      owner_gid   = 999
      permissions = "0755"
    }
  }

  tags = { Name = "${local.name}-redis" }
}

# The hatchet engine generates its own config on first boot -- including the
# encryption keyset that signs and validates every client token -- and writes
# it to /config. docker-compose.yml backs that with a named volume
# (`hatchet_config:/config`), which is why its documented bootstrap command is
# `/hatchet-admin --config /config token create ...`: the admin CLI reads the
# keys the server wrote.
#
# ECS had no volume here at all, and the consequences were not subtle. Verified
# live on a go-live rehearsal (2026-09-18): /config does not exist in the image
# (a one-off `ls -ldn /config` returned "No such file or directory"), so the
# entrypoint creates it in the container layer on every boot, generates a FRESH
# keyset into it, and loses it when the task stops. That means:
#
#   * any client token stops validating the next time the engine restarts, and
#     this deployment restarts it nightly on the EventBridge power sweep;
#   * whatever the engine encrypted into its Postgres database is unreadable
#     afterwards, because the key that encrypted it is gone;
#   * a token could not be minted AT ALL. The keys live only inside the running
#     container, ECS Exec is off by deliberate posture (rotation.tf), and a
#     one-off `hatchet-admin token create` task gets its own empty /config --
#     which is exactly the "could not load encryption service: encryption is
#     required" the rehearsal hit.
#
# All 51 workflows and every cron sit on top of that, so this is the store that
# looked stateless and was not.
#
# posix_user is root because the image runs as root: the same one-off task
# reported `uid=0(root) gid=0(root)`. Not assumed from the other two access
# points above, which are 1000 and 999 precisely because each image differs.
resource "aws_efs_access_point" "hatchet" {
  file_system_id = aws_efs_file_system.this.id

  posix_user {
    uid = 0
    gid = 0
  }

  root_directory {
    path = "/hatchet"
    creation_info {
      owner_uid   = 0
      owner_gid   = 0
      permissions = "0755"
    }
  }

  tags = { Name = "${local.name}-hatchet" }
}

# ---------------------------------------------------------------------------
# S3 — bronze, exports, backups
# ---------------------------------------------------------------------------
# Bronze was the one irreplaceable copy on Azure: no backup workflow (the
# backup_* workflows were deleted 2026-08-23) and no restore procedure, with
# Azure PITR covering Postgres only. On S3 the fix is configuration, so this
# deployment takes it rather than carrying the gap across.

resource "aws_s3_bucket" "this" {
  for_each = toset(["bronze", "bronze-raster", "exports", "backups"])
  bucket   = "${local.name}-${each.key}-${data.aws_caller_identity.current.account_id}"
}

data "aws_caller_identity" "current" {}

resource "aws_s3_bucket_versioning" "this" {
  for_each = aws_s3_bucket.this
  bucket   = each.value.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "this" {
  for_each = aws_s3_bucket.this
  bucket   = each.value.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "this" {
  for_each                = aws_s3_bucket.this
  bucket                  = each.value.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "this" {
  for_each = aws_s3_bucket.this
  bucket   = each.value.id

  rule {
    id     = "expire-old-versions"
    status = "Enabled"

    filter {}

    # Versioning is the backup. 90 days of non-current versions is what
    # makes "someone overwrote a bronze object" recoverable — the case
    # that had no answer at all on Azure.
    noncurrent_version_expiration {
      noncurrent_days = 90
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  rule {
    id     = "tier-cold-objects"
    status = "Enabled"

    # Bronze only. Exports are read soon after they are written or never,
    # and backups are already infrequent-access by nature.
    filter {
      prefix = "reports/"
    }

    transition {
      days          = 90
      storage_class = "STANDARD_IA"
    }
  }
}

# The `storage_tiering_run` Phase 0 agent moves bronze objects between
# hot/warm/cold tiers itself (Ch 08 §13). The lifecycle rule above is a
# floor under it, not a replacement: the agent writes to a literal
# `tier-warm` prefix that no Azure container ever had (Ch 02 §4), so
# nothing has been moving. Left as a floor deliberately — fixing the agent
# is not a migration task.
