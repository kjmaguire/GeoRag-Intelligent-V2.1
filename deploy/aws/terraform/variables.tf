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

variable "image_tag" {
  description = <<-EOT
    The tag every task definition pulls from ECR.

    THIS HAS NO DEFAULT ON PURPOSE. It used to be a hardcoded `:latest`,
    inherited from docker-compose.yml where `georag/laravel:latest` is what a
    local `docker build` leaves behind. That convention cannot survive the trip
    to ECR, because `aws_ecr_repository.this` sets
    `image_tag_mutability = "IMMUTABLE"`: a tag can be written once and never
    moved, so `latest` could never track anything. Nothing ever pushed it
    either — cd.yml pushes `:<short-sha>` and only that, and docker-build.yml
    pushes to GHCR, not here. Every task definition in this tree named a tag
    that did not exist and never would.

    What that cost, by resource:

      * the ten services and `georag-migrate` recovered, because cd.yml
        re-registers each one with `:<short-sha>` before it rolls. The damage
        was confined to the window between the first apply and the first
        deploy, where every task fails `CannotPullContainerError`, the
        deployment circuit breaker trips with no previous revision to roll
        back to, and the ALB alarms fire on a stack that looks applied.
      * `georag-app-key-rotation` did NOT recover. Nothing re-registers it —
        rotate-app-key.sh overrides `command` only, and ECS RunTask cannot
        override an image at all — so the APP_KEY rotation would have failed
        on an unpullable image the first time anyone ran it, in the middle of
        a procedure that has already taken the platform down.

    Pass the short SHA of an image cd.yml has pushed. On the very first apply
    of a fresh account no image exists yet, so pass anything (`bootstrap` is
    the conventional placeholder) and expect the services to stay down until
    the first deploy — then re-apply with a real SHA so the rotation task
    points at something pullable.
  EOT
  type        = string

  validation {
    condition     = var.image_tag != "latest"
    error_message = "image_tag must not be \"latest\": ECR repositories here are IMMUTABLE, nothing pushes that tag, and a task definition naming it can never start. Pass a short SHA that cd.yml has pushed."
  }

  validation {
    condition     = can(regex("^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$", var.image_tag))
    error_message = "image_tag must be a valid OCI tag: alphanumerics, dots, underscores and hyphens, not starting with a separator."
  }
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
  description = <<-EOT
    RDS instance class. Dropped from db.m7g.large to db.t4g.small on
    2026-09-15 for cost: $0.18/hour to $0.032/hour, about $27/month at the
    hours the AWS credit buys (see budget.tf). That is the second largest
    saving available after Fargate Spot.

    WHAT THE SMALLER CLASS COSTS, so the trade is re-litigated on evidence
    rather than rediscovered:

      * max_connections scales with memory on RDS
        (LEAST({DBInstanceClassMemory/9531392}, 5000)), so 8 GiB -> ~901 and
        2 GiB -> ~225. There is NO PgBouncer in this deployment — the ECS
        service list has no pooler and Laravel connects direct — so that is
        the real ceiling. Against it, the measured 24-hour peak on the Azure
        server was 99 connections (MIGRATION-PLAN §"laravel-octane-cc"),
        leaving ~2.3x headroom instead of ~9x. Fine for a pre-launch
        platform with one user; the first thing to suspect if connections
        start being refused under ingestion load.
      * t4g is BURSTABLE. CPU is credit-limited, and a long ingestion run can
        exhaust the credits and get throttled. A steady workload that would
        have been fine on m7g can crawl here.

    If either bites, db.t4g.medium is the middle step: 4 GiB, ~450
    connections, twice the CPU credit accrual, ~$0.065/hour — still less than
    half of m7g.large. Go back to db.m7g.large when there are real users and
    the credit is no longer what funds this.
  EOT
  type        = string
  default     = "db.t4g.small"
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

variable "db_deletion_protection" {
  description = <<-EOT
    RDS deletion protection. `true` is the resting state and the right one:
    it is what stands between a mistyped `terraform destroy` and the only
    copy of the database.

    IT ALSO BLOCKS THE POWER FLAG, which is why this is a variable rather
    than a hardcoded `true`. Terraform does not disable deletion protection
    on its way to deleting something, and setting `count = 0` does not help —
    the attribute change is never applied to a resource that is going away.
    So `terraform apply -var power=off` fails on the RDS destroy with
    "Cannot delete protected DB Instance" unless protection came off in an
    EARLIER apply:

      terraform apply -var power=on -var db_deletion_protection=false
      terraform apply -var power=off -var db_deletion_protection=false

    The first is an in-place attribute change with no downtime. The
    precondition on `aws_db_subnet_group` fails the PLAN if you skip it, so
    the failure arrives before anything is destroyed rather than half way
    through. Setting it false shows up in the plan, which is the point:
    turning the seatbelt off stays a deliberate, visible act.
  EOT
  type        = bool
  default     = true
}

variable "db_final_snapshot_suffix" {
  description = <<-EOT
    Appended to the final snapshot name, which is otherwise
    `<name_prefix>-pg-final`.

    RDS snapshot identifiers are UNIQUE PER ACCOUNT. A fixed name works once
    and then collides: the second `-var power=off` fails because the snapshot
    the first one wrote is still there. That is invisible until the second
    power cycle, which is exactly when a demo schedule hits it.

    Leave empty for a single on/off cycle. For a repeating cadence pass a
    date, so each teardown leaves its own restorable snapshot:

      terraform apply -var power=off -var db_final_snapshot_suffix=$(date +%Y%m%d)

    Old snapshots are NOT cleaned up by Terraform — they are manual snapshots
    and outlive `db_backup_retention_days` deliberately. Delete the ones you
    no longer want by hand; snapshot storage is billed on the data actually
    in them.
  EOT
  type        = string
  default     = ""

  validation {
    condition     = can(regex("^[a-zA-Z0-9-]*$", var.db_final_snapshot_suffix))
    error_message = "db_final_snapshot_suffix must be letters, digits and hyphens only — it becomes part of an RDS snapshot identifier."
  }
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

# ---------------------------------------------------------------------------
# Cohere's own API — chat and OCR (ADR-0023)
# ---------------------------------------------------------------------------
# `bedrock_chat_endpoint_name` and `bedrock_parse_endpoint_name` used to live
# here, both with no default, and they are gone. Command A+ and Parse 5 are
# AWS *Marketplace* SageMaker packages rather than Bedrock models — A100/H100
# and ~$2.50/hour, billing whether or not anything calls them, because a
# Marketplace endpoint has no idle state. Nothing was ever deployed, so no
# idle cost was incurred.
#
# Their removal takes the no-default tfvars from six to four, and with them
# the `<endpoint-name>-config` naming convention that would have taken out
# chat and OCR on a morning restart with nothing to alarm on.
#
# The credential is NOT here. COHERE_API_KEY is in Secrets Manager; putting
# it in a tfvar would put it in Terraform state.

variable "cohere_base_url" {
  description = <<-EOT
    Cohere API root. Override only for a proxy or a private deployment —
    the default is the public endpoint.
  EOT
  type        = string
  default     = "https://api.cohere.com"
}

variable "cohere_chat_model" {
  description = <<-EOT
    Cohere Command A+, the chat model (LLM_BACKEND=cohere). A plain model
    name, not an endpoint ARN: there is no endpoint indirection on this
    host, which is the difference ADR-0023 was chosen for.
  EOT
  type        = string
  default     = "command-a-plus-05-2026"
}

variable "cohere_parse_model" {
  description = <<-EOT
    Cohere Parse 5, the scanned-page OCR model (OCR_ENGINE=cohere_parse).
    The wire shape has never been empirically verified on any host — run
    the probe before trusting it (ADR-0019/0022/0023).
  EOT
  type        = string
  default     = "parse-v5.0"
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
# Observability
# ---------------------------------------------------------------------------

variable "container_insights" {
  description = <<-EOT
    CloudWatch Container Insights on the ECS cluster: "disabled", "enabled" or
    "enhanced".

    Defaults to DISABLED, changed from "enhanced" on 2026-09-16. The enhanced
    tier bills per observation and nothing consumed it: all fifteen alarms in
    alerts.tf read AWS/ApplicationELB, AWS/RDS, AWS/Bedrock or the custom
    GeoRAG/Markers namespace, and none reads ECS/ContainerInsights. The exact
    monthly figure is not recorded here because it has never been observed on
    this account — check Cost Explorer under CloudWatch after a day of running
    rather than trusting a number nobody measured.

    THE GAP THIS LEAVES, stated plainly, because it was equally open before:
    nothing alarms on an ECS task that is crash-looping or wedged. Container
    health checks (services.tf) detect it and ECS replaces the task, but no
    human is told. `HealthyHostCount` covers laravel-octane and laravel-reverb
    only — the two behind the ALB — so a hatchet-worker OOM-restarting every
    four minutes is silent, which is the exact shape Ch 12 §6 flags as
    "ingestion has stopped moving" and sends you to the logs for.

    Closing it does NOT require this setting: AWS/ECS carries per-service
    CPUUtilization and MemoryUtilization for free. That is a deliberate
    follow-up, not a silent omission.
  EOT
  type        = string
  default     = "disabled"

  validation {
    condition     = contains(["disabled", "enabled", "enhanced"], var.container_insights)
    error_message = "container_insights must be \"disabled\", \"enabled\" or \"enhanced\"."
  }
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

variable "monthly_budget_usd" {
  description = <<-EOT
    The monthly spend line that budget.tf alerts against, in USD. Defaults to
    100, which is the AWS promotional credit granted per month — so the
    default makes "over budget" mean "now spending real money".

    This ALERTS, it does not cap. AWS has no hard spending limit for ordinary
    accounts; acting on the alert is a human running
    `terraform apply -var power=off`.
  EOT
  type        = number
  default     = 100

  validation {
    condition     = var.monthly_budget_usd > 0
    error_message = "monthly_budget_usd must be greater than zero."
  }
}

variable "tags" {
  type    = map(string)
  default = {}
}
