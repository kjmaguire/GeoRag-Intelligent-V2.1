# Observability (ADR-0022).
#
# A rewrite, not a port — Azure Monitor and Log Analytics have no AWS
# equivalent to translate. What DOES carry across is every threshold, which
# was measured rather than guessed, and the two traps that made an Azure
# rule silently useless. Read Ch 12 before adding anything here.
#
# THE DESIGN, AS BUILT, IS LOG MARKERS. Nothing scrapes the two /metrics
# endpoints that exist — not on Azure, not here. `answer_quality_watch` was
# written in explicit response to that: rather than shipping counters to a
# metrics backend, it reads the same facts from `silver.answer_runs` and
# emits a distinctive log line. That chain — Postgres, worker log, log
# rule, email — is the production alerting design. Metric filters below are
# the AWS half of it.
#
# ONE IMPROVEMENT THAT COMES FREE. Azure matched these markers with
# SCHEDULED QUERY rules, which cost money per evaluation. A CloudWatch
# metric filter turns a log pattern into a metric at ingest with no
# per-query cost, and `treat_missing_data = "breaching"` expresses the
# dead-man signal that the Azure restart counter could not.
#
# ONE TRAP THAT DOES NOT APPLY, AND ONE THAT DOES.
#   - Container App JOBS left `ContainerAppName_s` empty and put the name
#     in `ContainerJobName_s`, so a rule written the obvious way parsed,
#     ran, cost money and matched nothing, silently, forever. ECS log
#     groups do not have that split — the sweeps write to their own group.
#     Every query in create-alerts.sh was nonetheless executed against the
#     live workspace before being written down, and the AWS equivalents
#     get the same treatment: run each pattern against real log data
#     before trusting the alarm.
#   - stdout is block-buffered until the process exits, so stdout
#     timestamps cluster at the moment a container died. That is a
#     property of the runtime, not of Azure, and it survives the move.
#     It is why the sweeps log to stderr and why these filters find
#     anything at all.

resource "aws_sns_topic" "alerts" {
  name = "${local.name}-alerts"
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

locals {
  alarm_actions = [aws_sns_topic.alerts.arn]

  # Marker lines the workers emit, and what each one means. Ch 12 §1.3 is
  # the authority; the thresholds inside the emitting workflows are not
  # duplicated here, deliberately — a threshold in two places drifts.
  log_markers = {
    answer-quality-regression = {
      pattern     = "ANSWER_QUALITY_REGRESSION"
      description = "answer_quality_watch (30 14 * * *): refusal, guard-fire or zero-evidence rate moved 15pp against the trailing week, or mean confidence dropped 0.15. A window under the 20-answer sample floor reports insufficient_sample and is deliberately NOT an alert."
    }
    cost-burn-threshold-exceeded = {
      pattern     = "COST_BURN_THRESHOLD_EXCEEDED"
      description = "cost_burn_watcher (*/5 * * * *): a workspace spent past its hourly ceiling. At 2x the watcher suspends its LLM activity by itself."
    }
    qdrant-partial-loss = {
      pattern     = "QDRANT_PARTIAL_LOSS"
      description = "embed_pending_passages sweep: Qdrant holds >2% fewer points for a project than silver.document_passages records as embedded."
    }
    bedrock-endpoint-not-inservice = {
      pattern     = "BEDROCK_ENDPOINT_NOT_INSERVICE"
      description = "NEW on AWS. A Marketplace endpoint failed to come back after the nightly delete. This leaves NO chat and NO OCR, and Bedrock's own invocation-error metrics cannot see it because there are no invocations to fail — the endpoint's absence is the whole failure. Sev 1."
    }
  }
}

resource "aws_cloudwatch_log_metric_filter" "markers" {
  for_each = local.log_markers

  name           = each.key
  log_group_name = aws_cloudwatch_log_group.services.name
  pattern        = "\"${each.value.pattern}\""

  metric_transformation {
    name      = each.key
    namespace = "GeoRAG/Markers"
    value     = "1"
    # Explicit zero, so "no marker" is a datapoint rather than a gap. An
    # alarm over a metric that only ever emits on failure sits in
    # INSUFFICIENT_DATA the rest of the time, which reads as broken.
    default_value = 0
  }
}

resource "aws_cloudwatch_metric_alarm" "markers" {
  for_each = local.log_markers

  alarm_name          = "${local.name}-${each.key}"
  alarm_description   = each.value.description
  namespace           = "GeoRAG/Markers"
  metric_name         = each.key
  statistic           = "Sum"
  period              = 900
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions

  depends_on = [aws_cloudwatch_log_metric_filter.markers]
}

# ---------------------------------------------------------------------------
# Scheduler sweeps
# ---------------------------------------------------------------------------
# Two rules, because one is not enough and Azure learned that the hard way:
# a sweep killed at its timeout emits no verdict at all, so a failure rule
# alone cannot see it. The dead-man rule below is the second half.

resource "aws_cloudwatch_log_metric_filter" "sweep_incomplete" {
  name           = "sweep-incomplete"
  log_group_name = aws_cloudwatch_log_group.scheduler.name
  pattern        = "?\"sweep INCOMPLETE\" ?\"FATAL:\""

  metric_transformation {
    name          = "sweep-incomplete"
    namespace     = "GeoRAG/Markers"
    value         = "1"
    default_value = 0
  }
}

resource "aws_cloudwatch_metric_alarm" "sweep_failed" {
  alarm_name          = "${local.name}-scheduler-sweep-failed"
  alarm_description   = "A nightly sweep reported a failed action or could not authenticate. Sev 1."
  namespace           = "GeoRAG/Markers"
  metric_name         = "sweep-incomplete"
  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions

  depends_on = [aws_cloudwatch_log_metric_filter.sweep_incomplete]
}

resource "aws_cloudwatch_log_metric_filter" "sweep_complete" {
  name           = "sweep-complete"
  log_group_name = aws_cloudwatch_log_group.scheduler.name
  pattern        = "\"sweep complete\""

  metric_transformation {
    name          = "sweep-complete"
    namespace     = "GeoRAG/Markers"
    value         = "1"
    default_value = 0
  }
}

resource "aws_cloudwatch_metric_alarm" "sweep_missing" {
  alarm_name        = "${local.name}-scheduler-sweep-missing"
  alarm_description = "No sweep verdict in 25 hours. The dead-man signal: a sweep killed at its timeout emits nothing at all, so the failure rule above cannot see it. Sev 1."

  namespace           = "GeoRAG/Markers"
  metric_name         = "sweep-complete"
  statistic           = "Sum"
  period              = 90000 # 25h — one full cycle plus an hour of slack
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  # The point of the rule. A missing datapoint IS the alarm condition.
  treat_missing_data = "breaching"
  alarm_actions      = local.alarm_actions

  depends_on = [aws_cloudwatch_log_metric_filter.sweep_complete]
}

# ---------------------------------------------------------------------------
# Public ingress
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "octane_5xx" {
  alarm_name        = "${local.name}-octane-5xx"
  alarm_description = "The only public service is returning 5xx. Azure had no availability or error-rate rule on its equivalent at all, despite Container Apps emitting the split for free."

  namespace           = "AWS/ApplicationELB"
  metric_name         = "HTTPCode_Target_5XX_Count"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 10
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions

  dimensions = {
    LoadBalancer = aws_lb.this.arn_suffix
    TargetGroup  = aws_lb_target_group.octane.arn_suffix
  }
}

resource "aws_cloudwatch_metric_alarm" "octane_dead_air" {
  alarm_name        = "${local.name}-octane-dead-air"
  alarm_description = <<-EOT
    No healthy Octane task. The restart counter Azure used could not tell
    "crash-looped and served nothing" from "restarted once and recovered";
    healthy-host count can.

    Dead air is the INTENDED state during the nightly window, which on
    Azure needed a separate suppression rule derived from the job crons.
    Here the alarm simply has no action during the window because the
    composite alarm below gates it — same outcome, one fewer place for the
    schedule to be spelled out.
  EOT

  namespace           = "AWS/ApplicationELB"
  metric_name         = "HealthyHostCount"
  statistic           = "Minimum"
  period              = 300
  evaluation_periods  = 2
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"

  dimensions = {
    LoadBalancer = aws_lb.this.arn_suffix
    TargetGroup  = aws_lb_target_group.octane.arn_suffix
  }
}

# The suppression window, derived from the schedule rather than written out
# again — the same discipline the Azure rule used, and for the same reason:
# the window was already spelled out in three places and did not need a
# fourth.
resource "aws_cloudwatch_composite_alarm" "octane_dead_air_outside_window" {
  alarm_name        = "${local.name}-octane-dead-air-alerting"
  alarm_description = "Dead air on the public service, outside the nightly maintenance window."

  alarm_rule    = "ALARM(${aws_cloudwatch_metric_alarm.octane_dead_air.alarm_name})"
  alarm_actions = local.alarm_actions

  # Suppressed while the platform is intentionally stopped. Without this the
  # alarm fires every single night by design, which is how an alert channel
  # becomes noise nobody reads.
  actions_suppressor {
    alarm            = aws_cloudwatch_metric_alarm.maintenance_window.alarm_name
    wait_period      = 60
    extension_period = 60
  }
}

resource "aws_cloudwatch_log_metric_filter" "shutdown_complete" {
  name           = "shutdown-sweep-complete"
  log_group_name = aws_cloudwatch_log_group.scheduler.name
  pattern        = "\"shutdown sweep complete\""

  metric_transformation {
    name          = "shutdown-sweep-complete"
    namespace     = "GeoRAG/Markers"
    value         = "1"
    default_value = 0
  }
}

resource "aws_cloudwatch_metric_alarm" "maintenance_window" {
  alarm_name        = "${local.name}-maintenance-window"
  alarm_description = <<-EOT
    In ALARM while the platform is intentionally stopped, suppressing the
    dead-air alarm.

    The signal is "the shutdown sweep reported complete within the window's
    own length", which is true from the moment the platform goes down until
    the moment it comes back up and false the rest of the day. The period is
    DERIVED from the two cron expressions (local.maintenance_window_hours)
    rather than written out again: the window was already spelled out in
    three places on Azure and did not need a fourth, which is the same
    reason the parity checker verified the cron against the DST guard.
  EOT

  namespace           = "GeoRAG/Markers"
  metric_name         = "shutdown-sweep-complete"
  statistic           = "Sum"
  period              = local.maintenance_window_hours * 3600
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  depends_on = [aws_cloudwatch_log_metric_filter.shutdown_complete]
}

# ---------------------------------------------------------------------------
# Bedrock
# ---------------------------------------------------------------------------
# These two are the reason Decision 1 is not purely a cost. Foundry blocked
# 1,421 of 2,524 calls on 2026-08-17 and nothing noticed, which is what
# earned `georag-foundry-cc-client-errors` its existence. Bedrock publishes
# the direct equivalents, so both rules port with their MEASURED thresholds
# intact rather than being rebuilt application-side — which is what calling
# a vendor API directly would have required.

resource "aws_cloudwatch_metric_alarm" "bedrock_client_errors" {
  alarm_name        = "${local.name}-bedrock-client-errors"
  alarm_description = "Bedrock rejected more than 50 calls in 15 minutes. Threshold carried from the Foundry rule, where it was set against a real incident."

  namespace           = "AWS/Bedrock"
  metric_name         = "InvocationClientErrors"
  statistic           = "Sum"
  period              = 900
  evaluation_periods  = 1
  threshold           = 50
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "bedrock_server_errors" {
  alarm_name        = "${local.name}-bedrock-server-errors"
  alarm_description = "More than 5 Bedrock server errors in 15 minutes. Threshold carried from the Foundry rule."

  namespace           = "AWS/Bedrock"
  metric_name         = "InvocationServerErrors"
  statistic           = "Sum"
  period              = 900
  evaluation_periods  = 1
  threshold           = 5
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "bedrock_throttles" {
  alarm_name        = "${local.name}-bedrock-throttles"
  alarm_description = <<-EOT
    Sustained Bedrock throttling. New on AWS: Foundry's shared per-
    subscription TPM quota produced transient 429s that were invisible
    until _foundry_retry existed, and before that a 429 was treated as a
    permanent per-batch failure and passages were SILENTLY SKIPPED from a
    corpus re-embed. botocore's adaptive retry handles the blips; this
    catches the case where retrying is not enough.
  EOT

  namespace           = "AWS/Bedrock"
  metric_name         = "InvocationThrottles"
  statistic           = "Sum"
  period              = 900
  evaluation_periods  = 2
  threshold           = 100
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
}

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "db_cpu" {
  alarm_name        = "${local.name}-pg-cpu"
  alarm_description = "Sustained Postgres CPU. Unlike the Azure equivalent, this one has query-level evidence behind it: log_min_duration_statement is set (see data.tf), which it was not on Flexible Server."

  namespace           = "AWS/RDS"
  metric_name         = "CPUUtilization"
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 3
  threshold           = 85
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions

  dimensions = { DBInstanceIdentifier = aws_db_instance.this.identifier }
}

resource "aws_cloudwatch_metric_alarm" "db_storage" {
  alarm_name        = "${local.name}-pg-storage"
  alarm_description = "Less than 10 GiB free. Storage autoscaling is on, so this is a warning that it is working, not that it is about to stop."

  namespace           = "AWS/RDS"
  metric_name         = "FreeStorageSpace"
  statistic           = "Minimum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 10 * 1024 * 1024 * 1024
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions

  dimensions = { DBInstanceIdentifier = aws_db_instance.this.identifier }
}

# NOTE, and it is the same note Ch 12 ends on: none of this measures answer
# quality. `answer_quality_watch` reading silver.answer_runs is the only
# thing that comes close, the two /metrics endpoints are still unscraped,
# and Laravel Pulse still collects data nobody can view in production. The
# cloud move neither fixes nor worsens any of that; it is recorded here so
# the gap stays visible rather than being rediscovered.
