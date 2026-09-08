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
# fire, no guard, no parity check for the cron half. What the parity check
# still earns its keep for is the OTHER half — that the deployed script
# matches the reviewed file — and that survives as
# scripts/check_sweep_task_parity.py, because an ECS RunTask override
# carries the same script twice for the same reason a Container Apps Job
# did.

resource "aws_ecs_task_definition" "shutdown_sweep" {
  family                   = "${local.name}-shutdown-sweep"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.scheduler_task.arn

  container_definitions = jsonencode([{
    name       = "sweep"
    essential  = true
    image      = "public.ecr.aws/aws-cli/aws-cli:latest"
    entryPoint = ["/bin/bash", "-c"]
    command    = [file("${path.module}/../scheduler/shutdown-sweep.sh")]
    environment = [
      { name = "SWEEP_CLUSTER", value = aws_ecs_cluster.this.name },
      { name = "SWEEP_DB_INSTANCE", value = aws_db_instance.this.identifier },
      {
        name  = "SWEEP_BEDROCK_ENDPOINTS"
        value = "${var.bedrock_chat_endpoint_name} ${var.bedrock_parse_endpoint_name}"
      },
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
  family                   = "${local.name}-startup-sweep"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.scheduler_task.arn

  container_definitions = jsonencode([{
    name       = "sweep"
    essential  = true
    image      = "public.ecr.aws/aws-cli/aws-cli:latest"
    entryPoint = ["/bin/bash", "-c"]
    command    = [file("${path.module}/../scheduler/startup-sweep.sh")]
    environment = [
      { name = "SWEEP_CLUSTER", value = aws_ecs_cluster.this.name },
      { name = "SWEEP_DB_INSTANCE", value = aws_db_instance.this.identifier },
      {
        # "name=config" pairs. The endpoint CONFIGS are never deleted, so
        # a recreate is one call rather than a rebuild.
        name = "SWEEP_BEDROCK_ENDPOINTS"
        value = join(" ", [
          "${var.bedrock_chat_endpoint_name}=${var.bedrock_chat_endpoint_name}-config",
          "${var.bedrock_parse_endpoint_name}=${var.bedrock_parse_endpoint_name}-config",
        ])
      },
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
  # rather than configured separately. `cron(0 23 * * ? *)` -> 23,
  # `cron(0 6 * * ? *)` -> 6, so the window is (6 - 23 + 24) % 24 = 7 hours.
  #
  # Deriving it is the point. On Azure the window was spelled out in the
  # shutdown cron, the startup cron, the DST guard's target hour and the
  # alert suppression rule, and `check_scheduler_job_parity.py` existed
  # partly to keep those in agreement. Two of those four are gone with the
  # guard; this keeps the fourth from coming back.
  shutdown_hour = tonumber(split(" ", replace(var.shutdown_cron, "/^cron\\(|\\)$/", ""))[1])
  startup_hour  = tonumber(split(" ", replace(var.startup_cron, "/^cron\\(|\\)$/", ""))[1])

  maintenance_window_hours = (local.startup_hour - local.shutdown_hour + 24) % 24
}

resource "aws_scheduler_schedule" "shutdown" {
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
      task_definition_arn = aws_ecs_task_definition.shutdown_sweep.arn_without_revision
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
      task_definition_arn = aws_ecs_task_definition.startup_sweep.arn_without_revision
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
