# Inputs for the GeoRAG AWS deployment (ADR-0022).
#
# Everything with no default is something a deployment MUST decide. The
# ones with defaults are the values production runs; changing them is a
# deliberate act, not a convenience.

variable "region" {
  description = "Region for every resource except Bedrock. See bedrock_region."
  type        = string
  default     = "us-east-1"
}

variable "bedrock_region" {
  description = <<-EOT
    Region for Bedrock calls. Separate from `region` because the Bedrock
    model catalogue varies by region and the models this deployment needs
    may not live where the rest of the stack does — confirming that is
    step 0 of the migration (ADR-0022). Empty inherits `region`.
  EOT
  type        = string
  default     = ""
}

variable "name_prefix" {
  description = "Prefix for every resource name."
  type        = string
  default     = "georag"
}

variable "vpc_cidr" {
  type    = string
  default = "10.40.0.0/16"
}

variable "az_count" {
  description = <<-EOT
    Availability zones to spread subnets across. Two is the minimum an ALB
    accepts; it is not high availability, because every service runs a
    single task and RDS is Single-AZ. Raising this alone does not make the
    platform redundant — see the module docs.
  EOT
  type        = number
  default     = 2
}

# ---------------------------------------------------------------------------
# Postgres
# ---------------------------------------------------------------------------

variable "db_instance_class" {
  type    = string
  default = "db.m7g.large"
}

variable "db_allocated_storage_gb" {
  type    = number
  default = 100
}

variable "db_engine_version" {
  description = <<-EOT
    PostgreSQL 18. The extension audit that made managed Postgres viable
    (ADR-0022) was done against 18: h3/h3_postgis are supported, and the
    only two extensions RDS lacks — pg_ivm and pg_stat_kcache — have zero
    call sites in this repository. Changing this major version invalidates
    that audit.
  EOT
  type        = string
  default     = "18.1"
}

variable "db_backup_retention_days" {
  description = <<-EOT
    35 matches what Azure Flexible Server was configured for, which is the
    only durability posture Postgres has ever had here. Note this covers
    Postgres ONLY: on Azure, blob storage had no backup and no restore
    procedure at all, which is why the S3 bucket below gets versioning and
    replication rather than inheriting that gap.
  EOT
  type        = number
  default     = 35
}

# ---------------------------------------------------------------------------
# Bedrock model ids
# ---------------------------------------------------------------------------
# No defaults on the two Marketplace endpoints: they are account-specific
# ARNs, and a placeholder that looks plausible is worse than a missing
# value that stops the plan.

variable "bedrock_embed_model_id" {
  type    = string
  default = "cohere.embed-v4:0"
}

variable "bedrock_rerank_model_id" {
  description = <<-EOT
    Cohere Rerank 3.5 — NOT v4, which Bedrock does not serve. The score
    threshold in RERANKER_SCORE_THRESHOLD_HOSTED was measured against v4
    and must be re-measured (ADR-0022).
  EOT
  type        = string
  default     = "cohere.rerank-v3-5:0"
}

variable "bedrock_chat_endpoint_name" {
  description = <<-EOT
    Name of the Bedrock Marketplace / SageMaker-managed endpoint serving
    Cohere Command A+. Bedrock's serverless generative catalogue is
    Command R/R+ (legacy), which is not the model this deployment runs.
    The nightly sweeps delete and recreate this endpoint, because it bills
    for as long as it exists.
  EOT
  type        = string
}

variable "bedrock_parse_endpoint_name" {
  description = <<-EOT
    Name of the Marketplace endpoint serving Cohere Parse 5. Bedrock's
    serverless catalogue carries no Parse model at all.
  EOT
  type        = string
}

# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------

variable "maintenance_timezone" {
  description = <<-EOT
    IANA timezone for the nightly window. This is the whole reason the DST
    double-fire guard could be deleted: Container Apps Jobs schedule in UTC
    only, so each Azure sweep fired at both candidate hours with an
    in-script guard exiting 0 on the wrong one — and that guard was subtly
    wrong for two days a year until 2026-08-21. EventBridge Scheduler takes
    a timezone and fires once.
  EOT
  type        = string
  default     = "America/Los_Angeles"
}

variable "shutdown_cron" {
  description = "Local-time cron for the nightly shutdown sweep."
  type        = string
  default     = "cron(0 23 * * ? *)"
}

variable "startup_cron" {
  description = "Local-time cron for the morning startup sweep."
  type        = string
  default     = "cron(0 6 * * ? *)"
}

# ---------------------------------------------------------------------------
# Alerting
# ---------------------------------------------------------------------------

variable "alert_email" {
  description = <<-EOT
    The single address alarms are delivered to. One receiver, no paging —
    the same posture Azure had, recorded rather than improved so the gap
    stays visible (Ch 12).
  EOT
  type        = string
}

variable "tags" {
  type    = map(string)
  default = {}
}
