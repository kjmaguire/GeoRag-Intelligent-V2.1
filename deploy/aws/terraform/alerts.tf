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
  #
  # `log_group` is NOT decoration. This deployment writes to two groups —
  # /ecs/georag for the ten services, /ecs/georag/scheduler for the two
  # nightly sweeps (services.tf:10 and :15) — and a metric filter only ever
  # sees the group it is attached to. Every marker here was filtered on the
  # services group until 2026-09-15, including the one emitted solely by
  # deploy/aws/scheduler/startup-sweep.sh, so the Sev 1 alarm on the
  # sharpest edge in this deployment watched a log group the marker cannot
  # appear in, and could never have fired.
  #
  # scripts/check-log-marker-alarms.py fails the build when a marker's
  # filter and its emitter disagree.
  log_markers = {
    answer-quality-regression = {
      log_group   = "services"
      pattern     = "ANSWER_QUALITY_REGRESSION"
      description = "answer_quality_watch (30 21 * * * UTC): refusal, guard-fire or zero-evidence rate moved 15pp against the trailing week, or mean confidence dropped 0.15. A window under the 20-answer sample floor reports insufficient_sample and is deliberately NOT an alert."
    }
    cost-burn-threshold-exceeded = {
      log_group   = "services"
      pattern     = "COST_BURN_THRESHOLD_EXCEEDED"
      description = "cost_burn_watcher (*/5 * * * *): a workspace spent past its hourly ceiling. At 2x the watcher suspends its LLM activity by itself."
    }
    qdrant-partial-loss = {
      log_group   = "services"
      pattern     = "QDRANT_PARTIAL_LOSS"
      description = "embed_pending_passages sweep: Qdrant holds >2% fewer points for a project than silver.document_passages records as embedded."
    }
    cohere-parse-unrecognised-response = {
      # Emitted by src/fastapi/app/services/ingest/cohere_parse_client.py,
      # which runs in the fastapi and hatchet-worker services — the services
      # group, not the sweep group.
      log_group   = "services"
      pattern     = "COHERE_PARSE_UNRECOGNISED_RESPONSE"
      description = "Cohere Parse returned HTTP 200 with a body the response adapter does not recognise, so the page fell back to tesseract and extracted no tables. Parse's wire shape has never been verified empirically on any host (ADR-0019/0022/0023), making this the most likely way the model tier is wrong. No invocation-error metric can see it — the call SUCCEEDED. Sustained firing means the adapter disagrees with the API: run the probe and correct it from the report."
    }
    sparse-encoder-unavailable = {
      # Emitted by src/fastapi/app/agent/tools.py::search_documents, which
      # runs in the fastapi service — the services group.
      #
      # This is the one degradation in the retrieval path that produces NO
      # other signal anywhere. Global Invariant 11 says a sparse failure must
      # not fall back to a dense-only query, and it does not — but the
      # exception is caught and turned into an empty DocumentSearchResult,
      # so what the rest of the pipeline sees is "nothing matched". The
      # answer still streams, the container stays healthy, and hybrid
      # retrieval has quietly stopped matching the things only the sparse
      # leg matches: hole IDs, sample numbers, NTS codes, anything where the
      # exact token IS the query.
      #
      # `sparse` runs desired=1 on Fargate Spot (main.tf, spot.tf), so a Spot
      # reclamation produces exactly this window as a matter of routine
      # rather than as an outage. SPLADE++ has no hosted equivalent on any
      # cloud, so there is nothing to fail over to — which makes knowing
      # about it the entire mitigation.
      log_group   = "services"
      pattern     = "SPARSE_ENCODER_UNAVAILABLE"
      description = "The sparse leg of hybrid retrieval is failing, so every document search is returning empty and reading downstream as an empty corpus. Usually the `sparse` service: reclaimed by Spot, still loading SPLADE++ (it 503s until resident), or unreachable. Check that service before concluding anything about the corpus or the reranker — a query that returns nothing here looks identical to one whose answer genuinely is not in the data."
    }
    cohere-parse-rejected = {
      # Same emitter, same services group. Separate from the marker above
      # because they are different failures with different fixes: that one
      # is "we read the answer wrong", this one is "we never got an answer".
      #
      # This alarm got MORE load-bearing on 2026-09-15, not less. While
      # Parse ran on Bedrock, a refused call also raised
      # AWS/Bedrock InvocationClientErrors, so this marker was a backstop.
      # On Cohere's own API there is no AWS metric behind it at all —
      # CloudWatch cannot see a request that never went to AWS — so the log
      # line is the entire signal. Without this filter, an invalid or
      # unentitled key degrades every scanned page to tesseract silently,
      # which is exactly what Foundry did on 2026-08-17: 1,421 of 2,524
      # calls blocked, nothing noticed.
      log_group   = "services"
      pattern     = "COHERE_PARSE_REJECTED"
      description = "Cohere Parse refused the request (401/403/404/413/422). Every scanned page is falling back to tesseract, which extracts no tables. Usually COHERE_API_KEY: absent, invalid, or not entitled to Parse. Retryable statuses are NOT here — those are retried in the adapter and log at WARNING."
    }
    hatchet-token-expiring = {
      # Emitted by deploy/aws/scheduler/token-expiry-check.sh, the daily
      # token-check task (scheduler.tf), which logs to the SCHEDULER group.
      # Audit AWS-12, 2026-09-29: the token lapses 90 days after minting and
      # nothing renewed it or alarmed on it. The task logs the marker every
      # day it applies, so this emails daily until the token is rotated.
      log_group   = "scheduler"
      pattern     = "HATCHET_TOKEN_EXPIRING"
      description = "HATCHET_CLIENT_TOKEN expires within 21 days, has expired, or the daily check could not read its expiry. When it lapses every Hatchet worker and client fails auth at once while the engine looks healthy: all 51 workflows and every cron stop. Rotate with deploy/aws/rotation/rotate-hatchet-token.sh (ops/runbooks/secret-rotation.md §9); the log line in /ecs/georag/scheduler (stream prefix token-check) says which case this is."
    }
    # bedrock-endpoint-not-inservice was here until 2026-09-15. ADR-0022
    # called it the sharpest edge in this deployment: a Marketplace endpoint
    # that failed to come back after the nightly delete left NO chat and NO
    # OCR, with no invocation-error metric able to see it because there were
    # no invocations to fail.
    #
    # ADR-0023 removed the endpoints, so the alarm has nothing to watch.
    # Deleted rather than left in place: an alarm on a marker nothing can
    # emit sits at zero forever and reads as healthy, which is the exact
    # failure scripts/check-log-marker-alarms.py exists to catch.
  }
}

resource "aws_cloudwatch_log_metric_filter" "markers" {
  for_each = local.log_markers

  name = each.key
  log_group_name = (
    each.value.log_group == "scheduler"
    ? aws_cloudwatch_log_group.scheduler.name
    : aws_cloudwatch_log_group.services.name
  )
  pattern = "\"${each.value.pattern}\""

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
  for_each = local.on == 1 ? local.log_markers : {}

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
# CRITICAL log lines — the production-posture check, and anything else
# ---------------------------------------------------------------------------
# main.py::_assert_production_posture is the only thing in the system that
# reports a security control being off (GEORAG_ENV=production), and it does
# so at CRITICAL, with a docstring saying CRITICAL "pages via the
# georag-fastapi-critical alert". That alert existed on Azure only. Here
# nothing watched for it until 2026-09-29 (audit AWS-10 / API-5), so an
# empty COHERE_API_KEY, a disabled hallucination layer or rate limiting off
# was logged into a group nobody reads.
#
# Two terms, either matches:
#   GEORAG_POSTURE_CRITICAL  the stable token the posture lines carry (added
#                            on the FastAPI side in the same audit pass)
#   CRITICAL                 the level itself — the JSON formatter writes
#                            "level": "CRITICAL", Laravel's stderr stack
#                            writes production.CRITICAL — so every other
#                            CRITICAL site (sidecar auth, OCR engine
#                            misconfiguration, empty service key) pages too,
#                            and so would the posture line if the token were
#                            ever dropped.
# Every CRITICAL site in src/fastapi/app is a misconfiguration logged once per
# process, so this is not a noisy filter by construction. Keep it that way:
# CRITICAL means "a person must act", not "worse than ERROR".
#
# Deliberately NOT in log_markers: scripts/check-log-marker-alarms.py requires
# each marker to have exactly one emitter, and a level string has hundreds.
resource "aws_cloudwatch_log_metric_filter" "posture_critical" {
  name           = "posture-critical"
  log_group_name = aws_cloudwatch_log_group.services.name
  pattern        = "?\"GEORAG_POSTURE_CRITICAL\" ?\"CRITICAL\""

  metric_transformation {
    name          = "posture-critical"
    namespace     = "GeoRAG/Markers"
    value         = "1"
    default_value = 0
  }
}

resource "aws_cloudwatch_metric_alarm" "posture_critical" {
  count = local.on

  alarm_name          = "${local.name}-posture-critical"
  alarm_description   = "A service logged at CRITICAL. From FastAPI or the hatchet worker this is usually _assert_production_posture on boot: a security or grounding control is off (RATE_LIMIT_ENABLED, PROMPT_INJECTION_DELIMITING_ENABLED, a hallucination layer), COHERE_API_KEY is empty, or QDRANT_DOCUMENT_PROJECT_SCOPE is cross_project. Search /ecs/georag for GEORAG_POSTURE_CRITICAL, then CRITICAL, in the last 15 minutes; the line names the setting."
  namespace           = "GeoRAG/Markers"
  metric_name         = "posture-critical"
  statistic           = "Sum"
  period              = 900
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions

  depends_on = [aws_cloudwatch_log_metric_filter.posture_critical]
}

# ---------------------------------------------------------------------------
# ECS tasks that crash, fail health checks, or cannot start
# ---------------------------------------------------------------------------
# The gap variables.tf's container_insights description names (audit AWS-21,
# 2026-09-29): ECS replaces a task that crashes or fails its container health
# check, and no human is told. HealthyHostCount covers only the two ALB
# services, so a hatchet-worker OOM-restarting every four minutes — Ch 12 §6's
# "ingestion has stopped moving" — was silent.
#
# EventBridge receives every ECS task state change for free. The rule keeps
# only SERVICE tasks (group "service:*" — not the migrate, smoke, sweep or
# token-check one-offs, which exit on purpose) that stopped because:
#   * the essential container exited on its own (a crash, an OOM kill), or
#   * the task never started (image pull, missing secret key, no Spot
#     capacity), or
#   * ECS stopped it for failing a container or ELB health check.
# Scale-ins (the nightly sweep), deployments and Spot interruptions stop
# tasks with other codes and reasons, so they do not match.
#
# Events go to a log group rather than straight to SNS so the alarm, not the
# event stream, decides when to email: one message per episode (two or more
# in 15 minutes) instead of one per restart. The group also keeps the
# stoppedReason of every one, which is the first thing to read:
#   aws logs tail /aws/events/georag/task-stops --since 1h
resource "aws_cloudwatch_log_group" "task_stops" {
  # EventBridge's CloudWatch Logs target expects the /aws/events/ prefix.
  name              = "/aws/events/${local.name}/task-stops"
  retention_in_days = 30
}

data "aws_iam_policy_document" "events_to_task_stops" {
  statement {
    sid     = "EventBridgeWritesTaskStops"
    effect  = "Allow"
    actions = ["logs:CreateLogStream", "logs:PutLogEvents"]
    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com", "delivery.logs.amazonaws.com"]
    }
    resources = ["${aws_cloudwatch_log_group.task_stops.arn}:*"]
  }
}

resource "aws_cloudwatch_log_resource_policy" "events_to_task_stops" {
  policy_name     = "${local.name}-events-to-task-stops"
  policy_document = data.aws_iam_policy_document.events_to_task_stops.json
}

resource "aws_cloudwatch_event_rule" "service_task_failed" {
  name        = "${local.name}-service-task-failed"
  description = "A service task in the ${local.name} cluster crashed, failed a health check, or failed to start (audit AWS-21)."

  event_pattern = jsonencode({
    source        = ["aws.ecs"]
    "detail-type" = ["ECS Task State Change"]
    detail = {
      clusterArn = [aws_ecs_cluster.this.arn]
      lastStatus = ["STOPPED"]
      group      = [{ prefix = "service:" }]
      "$or" = [
        { stopCode = ["EssentialContainerExited", "TaskFailedToStart"] },
        { stoppedReason = [{ prefix = "Task failed" }] },
      ]
    }
  })
}

resource "aws_cloudwatch_event_target" "service_task_failed" {
  rule = aws_cloudwatch_event_rule.service_task_failed.name
  arn  = aws_cloudwatch_log_group.task_stops.arn

  depends_on = [aws_cloudwatch_log_resource_policy.events_to_task_stops]
}

resource "aws_cloudwatch_log_metric_filter" "service_task_failed" {
  name           = "service-task-failed"
  log_group_name = aws_cloudwatch_log_group.task_stops.name
  pattern        = "{ $.detail.lastStatus = \"STOPPED\" }"

  metric_transformation {
    name          = "service-task-failed"
    namespace     = "GeoRAG/Markers"
    value         = "1"
    default_value = 0
  }
}

resource "aws_cloudwatch_metric_alarm" "service_task_failed" {
  count = local.on

  alarm_name          = "${local.name}-service-task-crash-loop"
  alarm_description   = "Two or more ECS service tasks crashed, failed a health check or failed to start within 15 minutes. ECS keeps replacing them, so the service LOOKS present. Read the stoppedReason: aws logs tail /aws/events/${local.name}/task-stops --since 1h. A hatchet-worker loop means ingestion and every cron have stopped moving."
  namespace           = "GeoRAG/Markers"
  metric_name         = "service-task-failed"
  statistic           = "Sum"
  period              = 900
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions

  depends_on = [aws_cloudwatch_log_metric_filter.service_task_failed]
}

# ---------------------------------------------------------------------------
# Cohere Parse pages billed
# ---------------------------------------------------------------------------
# Audit AWS-15, 2026-09-29. PDF_PARSE_MODE=all (config.tf) sends every PDF
# page to Parse on Cohere's own API, billed per page. No AWS budget can see
# a Cohere invoice, and cost_burn_watcher sums only usage.usage_events, which
# ingestion does not write. So nothing noticed a bulk ingest of historical
# NI 43-101s.
#
# cohere_parse_client._meter_pages now logs one line per billed page carrying
# `parse_pages_billed`; this sums it per day. The threshold is PAGES, not
# dollars: this file does not know Cohere's per-page price, and a number
# guessed here would be wrong silently. Set it from the price on the account.
resource "aws_cloudwatch_log_metric_filter" "cohere_parse_pages" {
  name           = "cohere-parse-pages-billed"
  log_group_name = aws_cloudwatch_log_group.services.name
  pattern        = "{ $.parse_pages_billed > 0 }"

  metric_transformation {
    name          = "cohere-parse-pages-billed"
    namespace     = "GeoRAG/Markers"
    value         = "$.parse_pages_billed"
    default_value = 0
  }
}

resource "aws_cloudwatch_metric_alarm" "cohere_parse_pages" {
  count = local.on * (var.cohere_parse_daily_page_alarm > 0 ? 1 : 0)

  alarm_name          = "${local.name}-cohere-parse-pages"
  alarm_description   = "More than ${var.cohere_parse_daily_page_alarm} pages went to Cohere Parse in 24 hours, each billed on Cohere's account, where no AWS budget can see it. Usually a bulk ingest. Check which documents: search /ecs/georag for COHERE_PARSE_PAGES_BILLED. To stop the spend, set PDF_PARSE_MODE back to ocr_only in config.tf."
  namespace           = "GeoRAG/Markers"
  metric_name         = "cohere-parse-pages-billed"
  statistic           = "Sum"
  period              = 86400
  evaluation_periods  = 1
  threshold           = var.cohere_parse_daily_page_alarm
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions

  depends_on = [aws_cloudwatch_log_metric_filter.cohere_parse_pages]
}

# Audit VEN-11, 2026-09-29. A page whose Parse call is still 429/5xx after
# its retries falls back to Tesseract - lower-quality text, silently. The
# client logs COHERE_PARSE_THROTTLED (WARNING) for each such page; five in an
# hour means Parse is being throttled or is down, not one unlucky page.
resource "aws_cloudwatch_log_metric_filter" "cohere_parse_throttled" {
  name           = "cohere-parse-throttled"
  log_group_name = aws_cloudwatch_log_group.services.name
  pattern        = "\"COHERE_PARSE_THROTTLED\""

  metric_transformation {
    name          = "cohere-parse-throttled"
    namespace     = "GeoRAG/Markers"
    value         = "1"
    default_value = 0
  }
}

resource "aws_cloudwatch_metric_alarm" "cohere_parse_throttled" {
  count = local.on

  alarm_name          = "${local.name}-cohere-parse-throttled"
  alarm_description   = "Five or more PDF pages in an hour exhausted their Cohere Parse retries (429/5xx) and fell back to Tesseract, so their text is lower quality. Search /ecs/georag for COHERE_PARSE_THROTTLED: the line gives the HTTP status. Sustained 429s mean the account's Parse rate limit is too low for the ingest volume; re-ingest the affected documents once it clears."
  namespace           = "GeoRAG/Markers"
  metric_name         = "cohere-parse-throttled"
  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = 1
  threshold           = 5
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions

  depends_on = [aws_cloudwatch_log_metric_filter.cohere_parse_throttled]
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
  count = local.on

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
  count = local.on

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
  count = local.on

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
    LoadBalancer = aws_lb.this[0].arn_suffix
    TargetGroup  = aws_lb_target_group.octane[0].arn_suffix
  }
}

resource "aws_cloudwatch_metric_alarm" "octane_dead_air" {
  count = local.on

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
    LoadBalancer = aws_lb.this[0].arn_suffix
    TargetGroup  = aws_lb_target_group.octane[0].arn_suffix
  }
}

locals {
  # The two timings both dead-air composites suppress with; the Octane one
  # below says what each is for. One definition, because "the same suppressor"
  # is the whole point of having two composites: Octane and Reverb are stopped
  # and started in the same tier (shutdown-sweep.sh, startup-sweep.sh), so a
  # window that is right for one is right for the other.
  dead_air_wait_period      = 900
  dead_air_extension_period = 2700
}

# The suppression window, derived from the schedule rather than written out
# again — the same discipline the Azure rule used, and for the same reason:
# the window was already spelled out in three places and did not need a
# fourth.
resource "aws_cloudwatch_composite_alarm" "octane_dead_air_outside_window" {
  count = local.on

  alarm_name        = "${local.name}-octane-dead-air-alerting"
  alarm_description = "Dead air on the public service, outside the nightly maintenance window."

  alarm_rule    = "ALARM(${aws_cloudwatch_metric_alarm.octane_dead_air[0].alarm_name})"
  alarm_actions = local.alarm_actions

  # Suppressed while the platform is intentionally stopped. Without this the
  # alarm fires every single night by design, which is how an alert channel
  # becomes noise nobody reads.
  #
  # Both periods widened on 2026-09-29 (audit AWS-7), and each covers one end
  # of the window:
  #
  #   extension_period 2700 s (45 min) — the MORNING end. The suppressor is in
  #     ALARM for exactly the window's length after the shutdown-complete
  #     marker, so it released the moment the startup sweep FIRED, while the
  #     platform was still down: RDS starting, three tiers each waiting for
  #     services-stable (README: ~15 min), then two healthy ALB checks and a
  #     clean 5-minute HealthyHostCount period. With 60 s the composite
  #     emailed at about 08:32 every day. 45 min covers a slow start with
  #     margin; a platform still dead 45 min after the suppressor lets go is
  #     a real page (when that is, see the DST hour below).
  #   wait_period 900 s (15 min) — the EVENING end. The shutdown sweep now
  #     drains tier by tier (AWS-8), so "shutdown sweep complete" lands
  #     several minutes after Octane stopped; dead air can reach ALARM before
  #     the suppressor does. The composite now waits up to 15 min for it.
  #     Cost: a genuine daytime outage emails up to 15 min later than before
  #     (on top of the 10 min the dead-air alarm itself needs).
  #
  # The suppressor's own period carries an hour of DST slack since 2026-10-10
  # (local.maintenance_suppressor_minutes; scheduler.tf has the arithmetic).
  # The fall-back night is an hour longer than the schedule says, and without
  # the hour this paged about five minutes before the startup sweep fired on
  # 2026-11-01. The price is on every other morning: the suppressor lets go at
  # about 09:40 rather than 08:40, so the page for a platform that never came
  # up arrives at about 10:25 rather than 09:25.
  actions_suppressor {
    alarm            = aws_cloudwatch_metric_alarm.maintenance_window[0].alarm_name
    wait_period      = local.dead_air_wait_period
    extension_period = local.dead_air_extension_period
  }
}

# Reverb, the other service behind the ALB, and the only road an answer takes to
# the browser: horizon runs the query and Reverb carries every streamed frame to
# the page. With Reverb down the platform still answers and shows nothing, while
# Octane is healthy and every alarm above stays quiet. variables.tf has said
# since 2026-09-16 that HealthyHostCount covers laravel-reverb as well; until
# 2026-10-10 only Octane's target group had an alarm on it.
#
# Same shape as Octane's and for the same reasons: desired is 2 here too
# (main.tf), so "fewer than one healthy host" means BOTH tasks are out, which is
# an outage and not a deploy in flight (the 50% floor in services.tf keeps one up
# through a rollout); and missing data breaches, because with every task gone
# there is nothing left to report a healthy host, and silence must not read as
# health.
resource "aws_cloudwatch_metric_alarm" "reverb_dead_air" {
  count = local.on

  alarm_name        = "${local.name}-reverb-dead-air"
  alarm_description = <<-EOT
    No healthy Reverb task. Answers are produced but never reach the browser:
    the stream is carried over this service, and nothing else alarms on it.

    Dead air is the INTENDED state during the nightly window. The composite
    below gates it with the same suppressor as Octane's, so this alarm alone
    is expected to be in ALARM every night and is not what pages.
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
    LoadBalancer = aws_lb.this[0].arn_suffix
    TargetGroup  = aws_lb_target_group.reverb[0].arn_suffix
  }
}

# A composite of its own rather than a second clause on Octane's, so the email
# says which service is down.
resource "aws_cloudwatch_composite_alarm" "reverb_dead_air_outside_window" {
  count = local.on

  alarm_name        = "${local.name}-reverb-dead-air-alerting"
  alarm_description = "Dead air on the WebSocket service, outside the nightly maintenance window."

  alarm_rule    = "ALARM(${aws_cloudwatch_metric_alarm.reverb_dead_air[0].alarm_name})"
  alarm_actions = local.alarm_actions

  actions_suppressor {
    alarm            = aws_cloudwatch_metric_alarm.maintenance_window[0].alarm_name
    wait_period      = local.dead_air_wait_period
    extension_period = local.dead_air_extension_period
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
  count = local.on

  alarm_name        = "${local.name}-maintenance-window"
  alarm_description = <<-EOT
    In ALARM while the platform is intentionally stopped, suppressing the
    dead-air alarms.

    The signal is "the shutdown sweep reported complete within the window's
    own length", which is true from the moment the platform goes down until
    the moment it comes back up and false the rest of the day. The period is
    DERIVED, not written out again: the two cron expressions
    (local.maintenance_window_minutes) plus an hour of DST slack
    (local.dst_slack_minutes), because the night the clocks fall back is an
    hour longer than the schedule says.

    The period must cover the WHOLE window, not most of it. Any shortfall
    lands at the end, where the marker ages out while the platform is still
    down — so the suppressor releases, this alarm's dead air is real, and the
    page arrives every morning until someone silences the channel. That is
    why the derivation counts minutes: startup moved to 08:30 on 2026-09-16
    and an hour-granular window would have been thirty minutes short.
  EOT

  namespace           = "GeoRAG/Markers"
  metric_name         = "shutdown-sweep-complete"
  statistic           = "Sum"
  period              = local.maintenance_suppressor_minutes * 60
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
  count = local.on

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
  count = local.on

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
  count = local.on

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
  count = local.on

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

  dimensions = { DBInstanceIdentifier = local.db.identifier }
}

resource "aws_cloudwatch_metric_alarm" "db_storage" {
  count = local.on

  alarm_name        = "${local.name}-pg-storage"
  alarm_description = "Postgres free storage is under 15% of the initially allocated size. On the original volume that is just ahead of RDS storage autoscaling's 10%-free trigger; after autoscaling has grown the volume it means autoscaling has not acted (it waits 6 hours between changes). Check FreeStorageSpace and the instance's storage-modification events."

  namespace          = "AWS/RDS"
  metric_name        = "FreeStorageSpace"
  statistic          = "Minimum"
  period             = 300
  evaluation_periods = 1
  # 15% of var.db_allocated_storage_gb, in bytes: 3 GiB at the 20 GB default.
  # It was a flat 10 GiB until 2026-09-29 (audit AWS-18) — HALF of a 20 GB
  # volume, so it fired as soon as the database held 10 GB of ordinary data,
  # went back to OK every night when the stopped instance stopped reporting
  # (notBreaching), and re-emailed every morning. RDS publishes no
  # allocated-storage metric to divide by, so this is relative to the INITIAL
  # size and stays a fixed floor once autoscaling grows the volume. Past the
  # first growth it sits below the 10% autoscaling trigger, so from then on it
  # fires only when autoscaling has NOT acted — the case worth an email.
  threshold           = floor(var.db_allocated_storage_gb * 0.15 * 1024 * 1024 * 1024)
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions

  dimensions = { DBInstanceIdentifier = local.db.identifier }
}

# NOTE, and it is the same note Ch 12 ends on: none of this measures answer
# quality. `answer_quality_watch` reading silver.answer_runs is the only
# thing that comes close, the two /metrics endpoints are still unscraped,
# and Laravel Pulse still collects data nobody can view in production. The
# cloud move neither fixes nor worsens any of that; it is recorded here so
# the gap stays visible rather than being rediscovered.
