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
}

resource "aws_db_parameter_group" "this" {
  name   = "${local.name}-pg18"
  family = "postgres18"

  # The extensions RDS gates behind shared_preload_libraries. auto_explain
  # is here rather than in an init script because on RDS it is a parameter,
  # not a CREATE EXTENSION — the init scripts' `CREATE EXTENSION
  # auto_explain` has no RDS equivalent and no reader in this repository
  # either way.
  parameter {
    name         = "shared_preload_libraries"
    value        = "pg_stat_statements,pg_partman_bgw,auto_explain"
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
  identifier     = "${local.name}-pg"
  engine         = "postgres"
  engine_version = var.db_engine_version
  instance_class = var.db_instance_class

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

  backup_retention_period   = var.db_backup_retention_days
  copy_tags_to_snapshot     = true
  deletion_protection       = true
  skip_final_snapshot       = false
  final_snapshot_identifier = "${local.name}-pg-final"

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
