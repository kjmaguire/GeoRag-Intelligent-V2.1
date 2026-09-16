# ---------------------------------------------------------------------------
# Fargate Spot
# ---------------------------------------------------------------------------
# The largest single lever on the running cost. At the configured size the
# services are 13.5 vCPU and 30 GB, which is $0.68/hour on demand and about
# $0.20/hour on Spot. Against a $100/month credit and no cash budget behind
# it, that is the difference between roughly three hours a day of runtime and
# roughly six.
#
# WHAT IT COSTS, plainly. A Spot task can be reclaimed by AWS with a two
# minute warning, at any time, and a new one only starts when Spot capacity
# exists in the region. Two consequences worth knowing before switching:
#
#   * An interruption is a task restart, not a graceful drain. Anything
#     in-flight on that task dies with it. For `hatchet-worker` that means a
#     workflow step is retried, which Hatchet is built for. For
#     `laravel-reverb` it means connected websockets drop and reconnect.
#   * Capacity is not guaranteed. If Spot is short in the region, a task sits
#     PENDING rather than starting. On demand has no such failure mode.
#
# Neither is acceptable in front of a customer, which is what
# `on_demand_services` is for. Put `laravel-octane` in it for a demo and the
# user-facing tier runs on demand while everything behind it stays on Spot —
# most of the saving, none of the risk where it is visible.
#
# WHY THE DEFAULT IS SPOT. This deployment's budget is promotional credit with
# no cash line behind it (budget.tf). A default of on demand would be the
# safer engineering choice for a funded production system and the wrong one
# here: it would quietly halve the hours the credit buys, and the failure it
# protects against — a restarted task on a pre-launch platform with no
# customers — costs nothing today. Revisit when there are users.

variable "fargate_capacity" {
  description = <<-EOT
    "spot" runs tasks on FARGATE_SPOT, ~70% cheaper, interruptible with two
    minutes' notice. "on_demand" runs everything on FARGATE.

    Per-service exceptions go in `on_demand_services` rather than flipping
    this globally — switching the whole platform to on demand to protect one
    user-facing service gives up the saving on the nine that nobody sees.
  EOT
  type        = string
  default     = "spot"

  validation {
    condition     = contains(["spot", "on_demand"], var.fargate_capacity)
    error_message = "fargate_capacity must be \"spot\" or \"on_demand\"."
  }
}

variable "on_demand_services" {
  description = <<-EOT
    Service names that stay on FARGATE even when `fargate_capacity = "spot"`.
    The escape hatch for a demo: ["laravel-octane"] keeps the tier a customer
    actually touches off Spot, at roughly $0.08/hour, while the workers,
    Qdrant, Redis and the sparse model keep the discount.

    Names must match keys of the services map in main.tf; a typo would
    silently do nothing, so it is validated below.
  EOT
  type        = set(string)
  default     = []
}

locals {
  # One entry per service, so `capacity_provider_strategy` never has to
  # re-derive the rule inline.
  capacity_for = {
    for name, _ in local.services :
    name => (
      var.fargate_capacity == "on_demand" || contains(var.on_demand_services, name)
      ? "FARGATE"
      : "FARGATE_SPOT"
    )
  }

  # A name in `on_demand_services` that matches no service is a typo that
  # would otherwise fail silently — the operator believes a service is
  # protected from interruption and it is not. Fail the plan instead.
  unknown_on_demand = setsubtract(var.on_demand_services, keys(local.services))
}

