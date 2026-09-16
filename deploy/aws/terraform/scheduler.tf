# Nightly shutdown / morning startup (ADR-0022).
#
# THE WHOLE MECHANISM COLLAPSED HERE, AND THAT IS THE POINT.
#
# On Azure this was: two Container Apps Jobs, each firing at TWO candidate
# UTC hours (`0 6,7 * * *` and `0 13,14 * * *`) because Container Apps Jobs
# have no timezone support; an in-script DST guard exiting 0 on the wrong
# hour; `scripts/check_scheduler_job_parity.py` verifying that the cron and
# the guard still agreed; and the alert suppression window derived from the
# crons so the schedule was not spelled out a fourth time. The guard was
# also subtly wrong for two days a year until 2026-08-21, because it
# compared against midnight UTC rather than the real transition instant.
#
# EventBridge Scheduler takes an IANA timezone. One schedule each, one
# fire, no guard — so the cron half of the parity check has nothing left
# to check.
#
# The OTHER half the parity check earned its keep for — that the DEPLOYED
# script is the REVIEWED script — is gone too, and by construction rather
# than by a second checker. The Azure job YAML held a pasted copy of the
# script in its `args`, so a reviewed file and a shipped file could
# disagree; `command = [file(...)]` below reads the reviewed file itself
# at plan time. There is one copy. Keep it that way: if you are ever
# tempted to inline a sweep body here, you are reintroducing the exact
# drift the deleted script existed to police.

resource "aws_ecs_task_definition" "shutdown_sweep" {
  count = local.on

  family                   = "${local.name}-shutdown-sweep"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.scheduler_task.arn

  container_definitions = jsonencode([{
    name      = "sweep"
    essential = true
    # Pinned 2026-09-16: verified via the public.ecr.aws registry API that
    # tag 2.36.46 resolves to the identical manifest list digest as :latest
    # at pin time (amd64 sha256:696ad2e7f8aac020bbbeaa511713cc844131ce593b275fdb101285ab02f6cbca,
    # arm64 sha256:b6934b9f6091ede7ed37e016b7dceaecc9246ede87c974d27db66da5e394d8ea).
    # Re-verify against public.ecr.aws/aws-cli/aws-cli before bumping.
    image      = "public.ecr.aws/aws-cli/aws-cli:2.36.46"
    entryPoint = ["/bin/bash", "-c"]
    command    = [file("${path.module}/../scheduler/shutdown-sweep.sh")]
    # SWEEP_BEDROCK_ENDPOINTS is gone (ADR-0023). The sweep deleted
    # Marketplace endpoints overnight because they bill for as long as they
    # exist; there are none left. Services and the database are still swept.
    environment = [
      { name = "SWEEP_CLUSTER", value = aws_ecs_cluster.this.name },
      { name = "SWEEP_DB_INSTANCE", value = local.db.identifier },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.scheduler.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "shutdown"
      }
    }
  }])
}

resource "aws_ecs_task_definition" "startup_sweep" {
  count = local.on

  family                   = "${local.name}-startup-sweep"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.scheduler_task.arn

  container_definitions = jsonencode([{
    name      = "sweep"
    essential = true
    # Pinned 2026-09-16: see shutdown_sweep above for how this tag was verified.
    image      = "public.ecr.aws/aws-cli/aws-cli:2.36.46"
    entryPoint = ["/bin/bash", "-c"]
    command    = [file("${path.module}/../scheduler/startup-sweep.sh")]
    # SWEEP_BEDROCK_ENDPOINTS is gone (ADR-0023), and with it the
    # `<endpoint-name>-config` naming convention it encoded. That convention
    # was a trap: a config named anything else meant create-endpoint failed
    # on a morning restart, leaving no chat and no OCR with no invocation
    # metric to alarm on — ADR-0022 called it the sharpest edge in this
    # deployment. There is nothing left to recreate.
    environment = [
      { name = "SWEEP_CLUSTER", value = aws_ecs_cluster.this.name },
      { name = "SWEEP_DB_INSTANCE", value = local.db.identifier },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.scheduler.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "startup"
      }
    }
  }])
}

locals {
  # The maintenance window's length, derived from the two cron expressions
  # rather than configured separately. `cron(0 17 * * ? *)` -> 17:00,
  # `cron(30 8 * * ? *)` -> 08:30, so the window is 930 minutes — 15h30m.
  #
  # Deriving it is the point. On Azure the window was spelled out in the
  # shutdown cron, the startup cron, the DST guard's target hour and the
  # alert suppression rule, and `check_scheduler_job_parity.py` existed
  # partly to keep those in agreement. Two of those four are gone with the
  # guard; this keeps the fourth from coming back.
  #
  # MINUTES, NOT HOURS, since 2026-09-16. This was `(startup_hour -
  # shutdown_hour + 24) % 24` while both sweeps fired on the hour, which was
  # exact right up until `startup_cron` moved to 08:30 — at which point it
  # would have read 15 hours for a 15h30m window, expiring the dead-air
  # alarm's suppressor (alerts.tf) half an hour before the platform came
  # back and paging every single morning. Deriving the window is worth
  # nothing if the derivation quietly truncates.
  shutdown_fields = split(" ", replace(var.shutdown_cron, "/^cron\\(|\\)$/", ""))
  startup_fields  = split(" ", replace(var.startup_cron, "/^cron\\(|\\)$/", ""))

  # tonumber() fails the PLAN on a `*/15`-style field rather than producing a
  # nonsense window. A sweep has one fire time; see the `*_cron` variables'
  # validation blocks, which reject the shape before it reaches here.
  shutdown_minute_of_day = tonumber(local.shutdown_fields[1]) * 60 + tonumber(local.shutdown_fields[0])
  startup_minute_of_day  = tonumber(local.startup_fields[1]) * 60 + tonumber(local.startup_fields[0])

  maintenance_window_minutes = (local.startup_minute_of_day - local.shutdown_minute_of_day + 1440) % 1440

  # Fractional on purpose: 930 minutes is 15.5, and an operator reading the
  # output needs the half hour to be visible rather than floored away.
  maintenance_window_hours = local.maintenance_window_minutes / 60
}

resource "aws_scheduler_schedule" "shutdown" {
  count = local.on

  name                         = "${local.name}-shutdown"
  schedule_expression          = var.shutdown_cron
  schedule_expression_timezone = var.maintenance_timezone

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_ecs_cluster.this.arn
    role_arn = aws_iam_role.scheduler.arn

    ecs_parameters {
      task_definition_arn = aws_ecs_task_definition.shutdown_sweep[0].arn_without_revision
      launch_type         = "FARGATE"
      network_configuration {
        subnets          = aws_subnet.private[*].id
        security_groups  = [aws_security_group.tasks.id]
        assign_public_ip = false
      }
    }

    # A sweep that is going to fail will fail on its first attempt. The
    # Azure job had replicaRetryLimit 1, which meant a sweep killed at its
    # replicaTimeout ran a second time in full — and the timeout was 300s
    # while the sweep measured 301s wall-clock, so three of five real runs
    # did exactly that.
    retry_policy {
      maximum_retry_attempts = 0
    }
  }
}

resource "aws_scheduler_schedule" "startup" {
  count = local.on

  name                         = "${local.name}-startup"
  schedule_expression          = var.startup_cron
  schedule_expression_timezone = var.maintenance_timezone

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_ecs_cluster.this.arn
    role_arn = aws_iam_role.scheduler.arn

    ecs_parameters {
      task_definition_arn = aws_ecs_task_definition.startup_sweep[0].arn_without_revision
      launch_type         = "FARGATE"
      network_configuration {
        subnets          = aws_subnet.private[*].id
        security_groups  = [aws_security_group.tasks.id]
        assign_public_ip = false
      }
    }

    retry_policy {
      maximum_retry_attempts = 0
    }
  }
}
