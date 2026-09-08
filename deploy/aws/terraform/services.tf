# ALB, task definitions, ECS services (ADR-0022).
#
# Ten services, one of them public. Task definitions here carry only the
# shape — image, resources, mounts, log config, health check — and get
# their environment from Secrets Manager plus the SSM parameters in
# config.tf. The point is that NOTHING is set by hand: the ~55 env vars per
# app that the Azure deployment set in the portal, and that drifted freely
# from .env.production.example, are the gap this file exists to close.

resource "aws_cloudwatch_log_group" "services" {
  name              = "/ecs/${local.name}"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "scheduler" {
  name              = "/ecs/${local.name}/scheduler"
  retention_in_days = 90
}

# ---------------------------------------------------------------------------
# Load balancer
# ---------------------------------------------------------------------------

resource "aws_lb" "this" {
  name               = local.name
  load_balancer_type = "application"
  subnets            = aws_subnet.public[*].id
  security_groups    = [aws_security_group.alb.id]

  # Reverb holds WebSocket connections open. The default 60s idle timeout
  # closes them between heartbeats, which the client reads as a dropped
  # connection and reconnects through — a reconnect storm that looks like
  # a Reverb fault and is not.
  idle_timeout = 300

  drop_invalid_header_fields = true
}

resource "aws_lb_target_group" "octane" {
  name        = "${local.name}-octane"
  port        = 80
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = aws_vpc.this.id

  health_check {
    path                = "/up"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    timeout             = 5
    interval            = 30
    matcher             = "200"
  }

  # Octane keeps no per-request state — session, cache and queue are all
  # Redis — so requests need not pin to a task. This is the difference
  # between it and Reverb below.
  deregistration_delay = 30
}

resource "aws_lb_target_group" "reverb" {
  name        = "${local.name}-reverb"
  port        = 8080
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = aws_vpc.this.id

  health_check {
    path                = "/up"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    timeout             = 5
    interval            = 30
    matcher             = "200,404"
  }

  # A WebSocket lives on ONE task for its whole life. Without stickiness a
  # reconnect can land on a different task than the one holding the
  # subscription, and the client silently stops receiving query stream
  # frames — the answer streams to nobody.
  stickiness {
    type            = "lb_cookie"
    enabled         = true
    cookie_duration = 86400
  }
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.this.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.acm_certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.octane.arn
  }
}

resource "aws_lb_listener_rule" "reverb" {
  listener_arn = aws_lb_listener.https.arn
  priority     = 10

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.reverb.arn
  }

  condition {
    path_pattern {
      values = ["/app/*", "/apps/*"]
    }
  }
}

resource "aws_lb_listener" "http_redirect" {
  load_balancer_arn = aws_lb.this.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"
    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}

# ---------------------------------------------------------------------------
# Task definitions
# ---------------------------------------------------------------------------

locals {
  # Which image each service runs. Three images, ten services — the same
  # relationship compose has, where laravel-octane/horizon/reverb and
  # fastapi/hatchet-worker/sparse each share a build.
  service_image = {
    laravel-octane  = "laravel"
    laravel-horizon = "laravel"
    laravel-reverb  = "laravel"
    fastapi         = "fastapi"
    hatchet-worker  = "fastapi"
    sparse          = "fastapi"
    martin          = "martin"
  }

  # Services running a third-party image rather than one of ours.
  external_image = {
    hatchet = "ghcr.io/hatchet-dev/hatchet/hatchet-lite:v0.86.12"
    qdrant  = "qdrant/qdrant:v1.17.1"
    redis   = "redis:8.10.0-alpine"
  }

  service_command = {
    laravel-octane  = ["php", "artisan", "octane:start", "--host=0.0.0.0", "--port=80"]
    laravel-horizon = ["php", "artisan", "horizon"]
    laravel-reverb  = ["php", "artisan", "reverb:start", "--host=0.0.0.0", "--port=8080"]
    hatchet-worker  = ["python", "-m", "app.hatchet_workflows.worker"]
    sparse          = ["uvicorn", "app.sparse_service:app", "--host", "0.0.0.0", "--port", "8000"]
    # AOF ON and a volume — the two halves of the fix for the one store in
    # this platform that genuinely had no persistence. `volatile-lru` is
    # non-negotiable and enforced by scripts/check_redis_manifests.py:
    # one instance holds queue jobs with no TTL beside TTL'd cache and
    # sessions, and `allkeys-lru` would evict a queued job.
    redis = [
      "redis-server",
      "--appendonly", "yes",
      "--appendfsync", "everysec",
      # Explicit, not omitted. Silence is NOT "off": Redis's built-in save
      # points stay active, so an AOF-only intent quietly runs RDB
      # snapshots as well. Dropping this line is precisely half of what
      # the Azure lift got wrong — it inherited `--appendonly yes` from
      # compose, did not inherit the volume, and did not inherit this
      # either. scripts/check_redis_manifests.py enforces both halves.
      "--save", "",
      "--maxmemory", "384mb",
      "--maxmemory-policy", "volatile-lru",
      "--databases", "4",
    ]
  }

  service_port = {
    laravel-octane = 80
    laravel-reverb = 8080
    fastapi        = 8000
    sparse         = 8000
    hatchet        = 7077
    qdrant         = 6333
    redis          = 6379
    martin         = 3000
  }
}

# ---------------------------------------------------------------------------
# Container health checks
# ---------------------------------------------------------------------------
# The ALB health-checks the two services behind it. Everything else — nine
# containers, including every store and both Hatchet halves — had NO liveness
# signal at all in the first draft of this deployment. Azure did better: it
# carried per-app probes in `probes.json`, and that file went with the Azure
# tree.
#
# What that costs is not theoretical. ECS only replaces a task whose PROCESS
# has exited; a process that is running and wedged is a healthy task forever.
# The Hatchet worker is the case that matters most, and Ch 12 §6 already named
# it: the SDK health server is "the probe that catches a hung worker holding
# its queue lease". A wedged worker holds leases, finishes nothing, and looks
# entirely fine — the exact symptom the on-call runbook's "ingestion has
# stopped moving" section sends you hunting for in the logs.
#
# These are the compose commands, unchanged, because they are already proven
# against these exact images. Two are worth reading twice:
#
#   qdrant  — speaks /readyz over bash's /dev/tcp because the image has no
#             curl and no wget. Do not "simplify" it to curl.
#   worker  — compose greps /proc/1/cmdline, which proves only that the
#             process exists. Production asks the SDK's health server
#             instead, which answers only while the event loop is turning
#             (config.tf enables it). That is the whole difference between
#             detecting a crash and detecting a hang.
locals {
  service_healthcheck = {
    redis           = ["CMD-SHELL", "redis-cli ping | grep -q PONG"]
    qdrant          = ["CMD", "bash", "-c", "exec 3<>/dev/tcp/localhost/6333 && printf 'GET /readyz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n' >&3 && grep -q '200 OK' <&3"]
    martin          = ["CMD", "wget", "--spider", "-q", "http://127.0.0.1:3000/health"]
    hatchet         = ["CMD-SHELL", "wget -q -O - http://localhost:8888/api/ready >/dev/null 2>&1 || exit 1"]
    hatchet-worker  = ["CMD-SHELL", "wget -q -O - http://localhost:8001/health >/dev/null 2>&1 || exit 1"]
    fastapi         = ["CMD", "curl", "-f", "http://localhost:8000/health"]
    sparse          = ["CMD", "curl", "-f", "http://localhost:8000/health"]
    laravel-octane  = ["CMD", "curl", "-f", "http://localhost:80/up"]
    laravel-horizon = ["CMD-SHELL", "php artisan horizon:status | grep -qE 'running|paused' || exit 1"]
    laravel-reverb  = ["CMD", "curl", "-f", "http://localhost:8080/up"]
  }

  # startPeriod is the grace before failures count. Hatchet runs its own
  # schema migration on boot (Azure allowed 45 s); the Laravel and FastAPI
  # images boot slower than the stores.
  healthcheck_start_period = {
    hatchet         = 90
    hatchet-worker  = 60
    fastapi         = 60
    laravel-octane  = 60
    laravel-horizon = 60
    laravel-reverb  = 60
  }
}

resource "aws_ecs_task_definition" "this" {
  for_each = local.services

  family                   = "${local.name}-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = each.value.cpu
  memory                   = each.value.memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  dynamic "volume" {
    for_each = contains(["qdrant", "redis"], each.key) ? [each.key] : []
    content {
      name = volume.value
      efs_volume_configuration {
        file_system_id     = aws_efs_file_system.this.id
        transit_encryption = "ENABLED"
        authorization_config {
          access_point_id = volume.value == "qdrant" ? aws_efs_access_point.qdrant.id : aws_efs_access_point.redis.id
          iam             = "ENABLED"
        }
      }
    }
  }

  container_definitions = jsonencode([
    merge(
      {
        name      = each.key
        essential = true
        image = lookup(
          local.external_image,
          each.key,
          "${aws_ecr_repository.this[lookup(local.service_image, each.key, "fastapi")].repository_url}:latest",
        )
        environment = [
          for k, v in local.service_environment[each.key] : { name = k, value = tostring(v) }
        ]
        secrets = local.service_secrets[each.key]
        logConfiguration = {
          logDriver = "awslogs"
          options = {
            "awslogs-group"         = aws_cloudwatch_log_group.services.name
            "awslogs-region"        = var.region
            "awslogs-stream-prefix" = each.key
          }
        }
      },
      lookup(local.service_command, each.key, null) != null ? {
        command = local.service_command[each.key]
      } : {},
      lookup(local.service_port, each.key, null) != null ? {
        portMappings = [{
          containerPort = local.service_port[each.key]
          protocol      = "tcp"
        }]
      } : {},
      contains(["qdrant", "redis"], each.key) ? {
        mountPoints = [{
          sourceVolume  = each.key
          containerPath = each.key == "qdrant" ? "/qdrant/storage" : "/data"
          readOnly      = false
        }]
      } : {},
      lookup(local.service_healthcheck, each.key, null) != null ? {
        healthCheck = {
          command     = local.service_healthcheck[each.key]
          interval    = 30
          timeout     = 5
          retries     = 3
          startPeriod = lookup(local.healthcheck_start_period, each.key, 30)
        }
      } : {},
    )
  ])
}

# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------

resource "aws_ecs_service" "this" {
  for_each = local.services

  name            = each.key
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.this[each.key].arn
  desired_count   = each.value.desired
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = false
  }

  service_registries {
    registry_arn = aws_service_discovery_service.this[each.key].arn
  }

  dynamic "load_balancer" {
    for_each = each.key == "laravel-octane" ? [1] : []
    content {
      target_group_arn = aws_lb_target_group.octane.arn
      container_name   = each.key
      container_port   = 80
    }
  }

  dynamic "load_balancer" {
    for_each = each.key == "laravel-reverb" ? [1] : []
    content {
      target_group_arn = aws_lb_target_group.reverb.arn
      container_name   = each.key
      container_port   = 8080
    }
  }

  # Replaces the hand-rolled "roll back to the previously-active digest"
  # logic in the Azure CD workflow, which had to shell out to read the
  # previous image and could only ever be best-effort.
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  # A single-task service cannot do a rolling deploy without going to
  # zero, so everything except Octane accepts a gap. Octane, at desired 2,
  # keeps one task serving throughout — which is the entire reason it is
  # not at 1 (ADR-0022 §3).
  deployment_minimum_healthy_percent = each.key == "laravel-octane" ? 50 : 0
  deployment_maximum_percent         = each.key == "laravel-octane" ? 200 : 100

  # The nightly sweeps own desired_count between 23:00 and 06:00 local.
  # Without this, every `terraform apply` during the window would start
  # the whole platform back up and quietly undo the cost saving.
  lifecycle {
    ignore_changes = [desired_count, task_definition]
  }

  depends_on = [aws_lb_listener.https]
}

# ---------------------------------------------------------------------------
# Schema task — migrations AND the raw SQL layer
# ---------------------------------------------------------------------------
# Run by CD before any service rolls. CD overrides the image to the SHA
# being deployed; everything else about the task lives here.
#
# THE `db:apply-raw` HALF IS THE POINT. Azure CD ran `php artisan migrate`
# and nothing else, so every object created only in `database/raw/` has
# never existed in production — while live code queries several of them
# (routers/interpretation.py reads all four interpretation.* tables; the
# phase0 ops agents read silver.corpus_health_findings and
# silver.store_reconciliation_findings; app/agent/egress_gate.py reads
# silver.workspace_settings). `scripts/raw-parity-baseline.txt` is the
# running list and `ops/runbooks/raw-sql-layer.md` the procedure.
#
# Order matters: migrations first, raw second. Several raw files are
# guarded on tables the migrations create, and every file in
# `database/raw/manifest.json` earns its place by being idempotent and
# re-run-safe — so running it every deploy is cheap, and running it never
# is how the gap opened.
#
# `--database=pgsql_migrations` is the connection the runbook specifies:
# the same one CD's migrations use, with the ownership split the
# `bootstrap` role needs. `db:apply-raw` has no `--force`; it is not
# interactive.
#
# ONE CAUTION, from ops/runbooks/raw-sql-layer.md. The phase-0 tenancy
# files in the manifest add columns, backfill them, flip them NOT NULL,
# add FK CASCADEs and enable RLS — they change DATA, not just shape. The
# runbook says to dry-run (`--pretend`) against any database that has not
# had them applied before, and to do the first real apply in a maintenance
# window with a confirmed restore point. On a FRESH deployment, which is
# what this migration is, there is no data to change and that caution does
# not bind. It binds again the first time this runs against a populated
# database.
resource "aws_ecs_task_definition" "migrate" {
  family                   = "${local.name}-migrate"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 1024
  memory                   = 2048
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([{
    name       = "migrate"
    essential  = true
    image      = "${aws_ecr_repository.this["laravel"].repository_url}:latest"
    entryPoint = ["/bin/sh", "-c"]
    command = [
      "php artisan migrate --force && php artisan db:apply-raw --database=pgsql_migrations",
    ]
    environment = [
      for k, v in merge(local.service_environment["laravel-octane"], {
        # Flips config/database.php's migrations.connection resolver to the
        # `pgsql_migrations` entry, which connects DIRECTLY rather than
        # through a pooler and as the owner role rather than the runtime
        # one. Unset, migrations silently run on `pgsql` as georag_app —
        # which lacks CREATE on the schemas and would fail late rather
        # than loudly.
        MIGRATE_DB_CONNECTION = "pgsql_migrations"
        MIGRATE_DB_HOST       = local.db_host
        MIGRATE_DB_PORT       = 5432
        MIGRATE_DB_USERNAME   = "georag"
      }) : { name = k, value = tostring(v) }
    ]
    secrets = concat(local.service_secrets["laravel-octane"], [{
      # RDS generates and rotates the master password into its own secret;
      # nothing in this repository or in CI ever holds it.
      name      = "MIGRATE_DB_PASSWORD"
      valueFrom = "${aws_db_instance.this.master_user_secret[0].secret_arn}:password::"
    }])
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.services.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "migrate"
      }
    }
  }])
}
