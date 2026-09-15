#!/usr/bin/env python3
"""Fail if the AWS power flag stops covering what it claims to cover.

`deploy/aws/terraform/power.tf` promises that `power = "off"` destroys
everything billed by the hour and keeps everything holding state. That promise
is only as good as the next resource somebody adds. A new `aws_lb`, a second
NAT gateway or an ElastiCache cluster added without `count = local.on` bills
around the clock in a deployment its owner believes is switched off, and
nothing reports it — the bill is the only signal, a month late.

So both halves are asserted, not just the gated one:

  * every resource of an hourly-billed TYPE carries the gate, and
  * every resource on the keep-list does NOT.

The second half matters as much as the first. Gating S3 or the Secrets
Manager secret would mean a power cycle destroys the corpus, or trips the
30-day `recovery_window_in_days` trap that power.tf exists to route around.

A new resource of an unknown type is reported too, so the choice is made
deliberately rather than by omission.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Overridable so the test can point it at fixtures; defaults to the real tree.
TF = (
    Path(sys.argv[1])
    if len(sys.argv) > 1
    else Path(__file__).resolve().parent.parent / "deploy" / "aws" / "terraform"
)

# Billed per hour (or per alarm) for as long as it exists. MUST be gated.
MUST_GATE = {
    "aws_nat_gateway",
    "aws_eip",
    "aws_route",
    "aws_lb",
    "aws_lb_target_group",
    "aws_lb_listener",
    "aws_lb_listener_rule",
    "aws_db_instance",
    "aws_ecs_service",
    "aws_ecs_task_definition",
    "aws_scheduler_schedule",
    "aws_cloudwatch_metric_alarm",
    "aws_cloudwatch_composite_alarm",
    "aws_service_discovery_service",
}

# State, or free, or painful to recreate. MUST NOT be gated. See power.tf.
MUST_NOT_GATE = {
    "aws_s3_bucket",
    "aws_s3_bucket_versioning",
    "aws_s3_bucket_server_side_encryption_configuration",
    "aws_s3_bucket_public_access_block",
    "aws_s3_bucket_lifecycle_configuration",
    "aws_efs_file_system",
    "aws_efs_mount_target",
    "aws_efs_access_point",
    "aws_secretsmanager_secret",
    "aws_secretsmanager_secret_version",
    "aws_ecr_repository",
    "aws_ecr_lifecycle_policy",
    "aws_cloudwatch_log_group",
    "aws_cloudwatch_log_metric_filter",
    "aws_sns_topic",
    "aws_sns_topic_subscription",
    "aws_vpc",
    "aws_subnet",
    "aws_internet_gateway",
    "aws_route_table",
    "aws_route_table_association",
    "aws_vpc_endpoint",
    "aws_security_group",
    "aws_ecs_cluster",
    "aws_service_discovery_private_dns_namespace",
    "aws_db_subnet_group",
    "aws_db_parameter_group",
    "aws_iam_role",
    "aws_iam_role_policy",
    "aws_iam_role_policy_attachment",
}

BLOCK = re.compile(r'^resource\s+"([a-z0-9_]+)"\s+"([a-z0-9_]+)"\s*\{', re.M)
GATED = re.compile(r"count\s*=\s*local\.on\b|for_each\s*=\s*local\.on\s*==\s*1\s*\?")


def blocks():
    for tf in sorted(TF.glob("*.tf")):
        text = tf.read_text()
        for m in BLOCK.finditer(text):
            # The block's own body, up to the next top-level resource.
            nxt = BLOCK.search(text, m.end())
            body = text[m.end() : nxt.start() if nxt else len(text)]
            yield tf.name, m.group(1), m.group(2), body


def main() -> int:
    ungated, wrongly_gated, unknown = [], [], []
    for fname, rtype, rname, body in blocks():
        addr = f"{rtype}.{rname}  ({fname})"
        gated = bool(GATED.search(body))
        if rtype in MUST_GATE:
            if not gated:
                ungated.append(addr)
        elif rtype in MUST_NOT_GATE:
            if gated:
                wrongly_gated.append(addr)
        else:
            unknown.append(addr)

    for label, items, why in (
        (
            "billed hourly but NOT gated",
            ungated,
            "add `count = local.on` (or `for_each = local.on == 1 ? ... : {}`); "
            "without it this bills while the platform is switched off",
        ),
        (
            "gated but must NOT be",
            wrongly_gated,
            "powering off would destroy state or trip the secret recovery "
            "window; see the header of power.tf",
        ),
        (
            "unrecognised resource type",
            unknown,
            "decide deliberately: add the TYPE to MUST_GATE or MUST_NOT_GATE "
            "in this script",
        ),
    ):
        if items:
            print(f"\n{len(items)} {label}:", file=sys.stderr)
            for a in items:
                print(f"  - {a}", file=sys.stderr)
            print(f"  -> {why}", file=sys.stderr)

    if ungated or wrongly_gated or unknown:
        return 1
    total = sum(1 for _ in blocks())
    print(
        f"AWS power flag: {total} resource(s); "
        f"every hourly-billed one gated, every stateful one kept."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
