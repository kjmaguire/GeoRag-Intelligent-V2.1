# ---------------------------------------------------------------------------
# The spend guard
# ---------------------------------------------------------------------------
# This deployment runs on AWS promotional credit: $100/month for six months,
# plus a one-off $100 for the account-setup activities. That is the entire
# budget — the Activate Founders application was REJECTED on 2026-09-15, so
# there is no larger pool behind it and no cash line to fall back on.
#
# The arithmetic that makes this file load-bearing: left running at its
# configured size this platform costs about $555/month, so ONE forgotten month
# burns five and a half months of credit. Even the trimmed configuration burns
# 1.3 months. The power switch (power.tf) is what makes the credit sufficient,
# and a power switch nobody notices was left ON is exactly how the credit
# disappears.
#
# NOT gated by `local.on`, deliberately, and for two reasons. A budget costs
# nothing (AWS charges for budgets beyond the first two; this is one). And a
# spend guard that is destroyed alongside the thing it guards is not a guard —
# the failure it exists to catch is precisely "the platform is running when I
# believed it was off", which is a state where a gated budget would already be
# gone. scripts/check-aws-power-flag.py enforces that it stays ungated.
#
# WHAT IT DOES NOT DO. A budget alerts; it does not cap. AWS has no hard
# spending limit for ordinary accounts, so nothing here can stop a runaway
# charge — it can only tell you. Acting on the alert means
# `terraform apply -var power=off`, and that is a human step. If the alerts
# below ever fire, treat the numbers as already spent.

# IF A BUDGET ALREADY EXISTS IN THE CONSOLE, deal with it before the first
# apply. Kyle created one by hand on 2026-09-15, which is the right thing to
# have done — it protected the account days before any Terraform ran — but
# budget names are unique per account, so the two can collide:
#
#   * Named `georag-monthly` (the name below): `terraform apply` FAILS with
#     a duplicate-name error. Adopt it instead of recreating it:
#
#       terraform import aws_budgets_budget.monthly <account-id>:georag-monthly
#
#     Then `terraform plan` shows the drift between the console settings and
#     the thresholds below, and the next apply reconciles it.
#   * Named anything else: both budgets exist and both email. Not an error
#     and not billed — AWS gives two budgets free — but duplicate alerts get
#     muted, and a muted spend alert is the failure this file exists to
#     prevent. Delete the console one, or import it under its own name.
#
# Deleting the console budget before the first apply is also fine. Nothing
# bills until `power=on`, so the window where neither exists is a window
# where there is nothing to overspend.
resource "aws_budgets_budget" "monthly" {
  name         = "${local.name}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  # Three thresholds, and the third is the one that matters.
  #
  # ACTUAL at 50% and 80% are review prompts: half the month's credit is gone,
  # then most of it. FORECASTED at 100% is the early warning — it fires on the
  # projected month-end total, so a stack left on over a weekend trips it days
  # before the credit is actually exhausted, while turning it off still helps.
  # An ACTUAL-only budget tells you after the money is gone.
  dynamic "notification" {
    for_each = [
      { type = "ACTUAL", threshold = 50 },
      { type = "ACTUAL", threshold = 80 },
      { type = "FORECASTED", threshold = 100 },
    ]

    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value.threshold
      threshold_type             = "PERCENTAGE"
      notification_type          = notification.value.type
      subscriber_email_addresses = [var.alert_email]

      # Email direct from Budgets, NOT through the alerts SNS topic. The
      # topic's subscription is confirmed by hand and its policy is written
      # for CloudWatch; routing spend alerts through it would add a way for
      # this to silently not deliver. A budget notification that does not
      # arrive is indistinguishable from a month that stayed under budget.
    }
  }
}
