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

THIRD: a gated resource must not carry an attribute that blocks its own
destruction. Gating something the provider then refuses to delete is worse
than not gating it — `-var power=off` gets as far as the ALB, the NAT gateway
and every ECS service before failing, leaving the stack half torn down.

Two shipped in the original power flag on 2026-09-15 and neither had ever
run, because there are no AWS credentials in CI and `terraform plan` has
never executed in either power state:

    deletion_protection = true        RDS refuses the delete. Terraform does
                                      not clear the flag on its way out, and
                                      count = 0 applies no attribute change
                                      to a resource that is going away.
    final_snapshot_identifier = <const>
                                      Works once. Snapshot identifiers are
                                      unique per account, so the SECOND
                                      power-off collides with the snapshot
                                      the first one wrote — invisible until
                                      the second cycle.

FOURTH: a policy document is not gated (the roles are free, so power.tf keeps
them across power cycles), which makes any list inside one that is built from
a GATED resource EMPTY under `power = "off"` -- and IAM rejects a statement
with no Resource. `resources = [for s in aws_ecs_service.this : s.id]` sat in
the scheduler role this way and would have failed the power-off apply part-way
through, at the policy update. Only `coalescelist(<loop>, [<match-nothing
ARN>])` survives; try() and compact() leave an empty list empty.
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
    # A distribution bills per request and per GB while it exists, and an
    # ungated one would keep answering for an origin that power=off deleted —
    # serving 502s under the platform's only public hostname.
    "aws_cloudfront_distribution",
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
    # The TLS-only deny policy (data.tf). Free, and it guards the buckets that
    # survive power=off, so it must survive with them.
    "aws_s3_bucket_policy",
    "aws_efs_file_system",
    "aws_efs_mount_target",
    "aws_efs_access_point",
    "aws_secretsmanager_secret",
    "aws_secretsmanager_secret_version",
    "aws_ecr_repository",
    "aws_ecr_lifecycle_policy",
    "aws_cloudwatch_log_group",
    "aws_cloudwatch_log_metric_filter",
    # The ECS task-failure route (alerts.tf, audit AWS-21): an EventBridge
    # rule on AWS service events, its log-group target and the resource
    # policy that lets EventBridge write there. None bills by the hour, and
    # with the cluster empty they simply see no events.
    "aws_cloudwatch_event_rule",
    "aws_cloudwatch_event_target",
    "aws_cloudwatch_log_resource_policy",
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
    # Attaching capacity providers to the cluster costs nothing and must
    # survive a power cycle: an unattached FARGATE provider is what makes
    # `-var fargate_capacity=on_demand` fail on the demo it was set for.
    "aws_ecs_cluster_capacity_providers",
    "aws_service_discovery_private_dns_namespace",
    "aws_db_subnet_group",
    "aws_db_parameter_group",
    "aws_iam_role",
    "aws_iam_role_policy",
    "aws_iam_role_policy_attachment",
    # Free identity/trust configuration, not hourly-billed infrastructure --
    # same category as the IAM roles above. Gating it would also break CD:
    # the GitHub Actions deploy role (ci.tf) needs to exist and authenticate
    # BEFORE a power=on apply can run, not be created by one.
    "aws_iam_openid_connect_provider",
    # The spend guard must outlive the thing it guards. The failure it exists
    # to catch is "the platform is running when I believed it was off" — a
    # state in which a gated budget would already have been destroyed.
    "aws_budgets_budget",
    # A public ACM certificate costs nothing to hold and is slow to replace:
    # gating it would put DNS validation on the critical path of every
    # power-on, for no saving. The record that ALIASES the ALB is a different
    # question and is answered per-address below.
    "aws_acm_certificate",
    "aws_acm_certificate_validation",
}

#: Per-ADDRESS decisions, checked BEFORE the type tables above.
#:
#: `aws_route53_record` is the first type to land legitimately on both sides,
#: which the type-level model could not express. The two records in dns.tf want
#: opposite answers, and getting either backwards is a real failure:
#:
#:   * the ALIAS record points at the ALB, which power=off destroys. Left
#:     ungated it dangles at a load balancer that no longer exists.
#:   * the VALIDATION records are what ACM re-reads to renew the certificate
#:     for as long as it lives. Gating them means a power cycle deletes them,
#:     and renewal fails silently months later.
#:
#: A route53 record at any OTHER address matches neither table and is reported
#: as unknown, so the next one is decided deliberately too.
ADDRESS_MUST_GATE = {
    "aws_route53_record.app",
}

ADDRESS_MUST_NOT_GATE = {
    "aws_route53_record.cert_validation",
}

BLOCK = re.compile(r'^resource\s+"([a-z0-9_]+)"\s+"([a-z0-9_]+)"\s*\{', re.M)
#: Any `count` expression that MENTIONS local.on is gated, not only one that
#: starts with it. `count = local.dns * local.on` is as gated as `count =
#: local.on`, and the previous anchored pattern read it as ungated — which
#: would have reported a correctly gated resource as a billing leak.
GATED = re.compile(
    r"count\s*=\s*[^\n]*\blocal\.on\b|for_each\s*=\s*local\.on\s*==\s*1\s*\?"
)

#: Attributes that stop the provider destroying the resource they sit on, and
#: what makes each one safe. Checked on GATED resources only — on an ungated
#: one, deletion protection is simply protection doing its job.
_LITERAL_TRUE = re.compile(r"=\s*true\b")
_PREVENT_DESTROY = re.compile(r"prevent_destroy\s*=\s*true\b")


def _parse_locals(text: str) -> dict[str, str]:
    """`local.<name>` -> its defining expression, across every locals block.

    A real (if small) parser rather than a substring test, because the
    question this has to answer is precise: does the snapshot name vary
    between two power cycles of the SAME deployment? A looser "does it
    mention any variable anywhere" test passes the very tree this rule was
    written for — `"${local.name}-pg-final"` reaches `var.name_prefix`, which
    is deployment identity and fixed by definition.
    """
    out: dict[str, str] = {}
    for m in re.finditer(r"^locals\s*\{", text, re.M):
        depth, i = 0, m.end() - 1
        while i < len(text):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        body = text[m.end() : i]
        starts = [a for a in re.finditer(r"^\s{2}([a-z0-9_]+)\s*=", body, re.M)]
        for n, a in enumerate(starts):
            end = starts[n + 1].start() if n + 1 < len(starts) else len(body)
            out[a.group(1)] = body[a.end() : end]
    return out


def _expand(expr: str, defs: dict[str, str], depth: int = 4) -> str:
    """Inline `local.X` references so the variables behind them are visible."""
    for _ in range(depth):
        refs = set(re.findall(r"local\.([a-z0-9_]+)", expr))
        if not refs & defs.keys():
            break
        for name in refs & defs.keys():
            expr = expr.replace(f"local.{name}", f" {defs[name]} ")
    return expr


def _vars_in(expr: str) -> set[str]:
    return set(re.findall(r"var\.([a-z0-9_]+)", expr))


def destroy_blockers(body: str, locals_text: dict[str, str]) -> list[str]:
    """Reasons `power=off` would fail to destroy this gated resource."""
    found = []

    for line in body.splitlines():
        stripped = line.strip()

        if stripped.startswith("deletion_protection") and _LITERAL_TRUE.search(stripped):
            found.append(
                "deletion_protection = true — RDS refuses the delete and the "
                "power-off fails part-way through. Make it a variable so it can "
                "be cleared in an earlier apply."
            )

        if stripped.startswith("final_snapshot_identifier"):
            value = stripped.split("=", 1)[1] if "=" in stripped else ""
            # Must depend on something BEYOND deployment identity. A name
            # built only from `local.name` is fixed for the life of the
            # deployment, which is exactly the collision case.
            identity = _vars_in(_expand("local.name", locals_text))
            if not (_vars_in(_expand(value, locals_text)) - identity):
                found.append(
                    "final_snapshot_identifier is a constant — snapshot names are "
                    "unique per account, so the SECOND power-off collides with the "
                    "first one's snapshot. Give it an operator-settable suffix."
                )

        if _PREVENT_DESTROY.search(stripped):
            found.append(
                "lifecycle.prevent_destroy = true — Terraform refuses to destroy "
                "this at all, so the gate on it can never take effect."
            )

    return found


POLICY_DOC = re.compile(r'^data\s+"aws_iam_policy_document"\s+"([a-z0-9_]+)"\s*\{', re.M)
_FOR_IN = re.compile(r"\bfor\s+[a-z0-9_]+(?:\s*,\s*[a-z0-9_]+)?\s+in\s+")


def _strip_comments(text: str) -> str:
    """Drop `#` and `//` comments, leaving string literals alone."""
    out = []
    for line in text.splitlines():
        quoted = False
        for i, ch in enumerate(line):
            if ch == '"' and line[i - 1 : i] != "\\":
                quoted = not quoted
            elif not quoted and (ch == "#" or line.startswith("//", i)):
                line = line[:i]
                break
        out.append(line)
    return "\n".join(out)


def _close(text: str, open_idx: int) -> int:
    """Index of the bracket that closes the one at `open_idx` (len if none)."""
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] in "([{":
            depth += 1
        elif text[i] in ")]}":
            depth -= 1
            if depth == 0:
                return i
    return len(text)


def _enclosing_calls(text: str, pos: int) -> list[tuple[str, int]]:
    """(name, index of "(") of every call whose arguments contain `pos`.

    String literals are skipped by toggling on `"`, so a quote nested inside a
    `${...}` interpolation is read loosely. Nothing in a policy document needs
    one, and a misread can only make a wrapper go unrecognised, which fails
    the check rather than passing it.
    """
    stack: list[tuple[str, int]] = []
    quoted = False
    for i in range(pos):
        ch = text[i]
        if ch == '"' and text[i - 1 : i] != "\\":
            quoted = not quoted
        elif quoted:
            continue
        elif ch in "([{":
            name = re.search(r"([a-z0-9_]+)\s*$", text[:i]) if ch == "(" else None
            stack.append((name.group(1) if name else "", i))
        elif ch in ")]}" and stack:
            stack.pop()
    return [(n, i) for n, i in reversed(stack) if n]


def _loop_source(doc: str, start: int) -> str:
    """The collection a `for ... in` iterates: from `start` to its `:`."""
    depth = 0
    for i in range(start, len(doc)):
        ch = doc[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth < 0:
                break
        elif ch == ":" and depth == 0:
            return doc[start:i]
    return doc[start:]


def _last_argument(args: str) -> tuple[int, str]:
    """(offset, text) of the last top-level argument of a call's argument list."""
    args = args.rstrip().rstrip(",")
    depth, quoted, start = 0, False, 0
    for i, ch in enumerate(args):
        if ch == '"' and args[i - 1 : i] != "\\":
            quoted = not quoted
        elif quoted:
            continue
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            start = i + 1
    return start, args[start:].strip()


def _survives_empty(doc: str, pos: int) -> bool:
    """True if the list at `pos` sits inside a coalescelist() that has a real
    fallback: the last argument is not `[]`, and is not where `pos` is."""
    for name, open_idx in _enclosing_calls(doc, pos):
        if name != "coalescelist":
            continue
        inner = open_idx + 1
        offset, fallback = _last_argument(doc[inner : _close(doc, open_idx)])
        if fallback and not re.fullmatch(r"\[\s*\]", fallback) and pos < inner + offset:
            return True
    return False


def unwrapped_gated_lists(gated: set[str]) -> list[str]:
    """Lists in an ungated policy document that go empty under power=off."""
    found = []
    for tf in sorted(TF.glob("*.tf")):
        text = _strip_comments(tf.read_text())
        for m in POLICY_DOC.finditer(text):
            doc = text[m.start() : _close(text, m.end() - 1) + 1]
            if GATED.search(doc):
                # A gated document does not exist under power=off either.
                continue
            hits = [
                (loop.start(), addr)
                for loop in _FOR_IN.finditer(doc)
                for addr in sorted(gated)
                if re.search(rf"\b{re.escape(addr)}\b", _loop_source(doc, loop.end()))
            ] + [
                (s.start(), addr)
                for addr in sorted(gated)
                for s in re.finditer(rf"\b{re.escape(addr)}\[\*\]", doc)
            ]
            found += [
                f"data.aws_iam_policy_document.{m.group(1)} builds a list from "
                f"{addr}  ({tf.name})"
                for pos, addr in hits
                if not _survives_empty(doc, pos)
            ]
    return found


def blocks():
    for tf in sorted(TF.glob("*.tf")):
        text = tf.read_text()
        for m in BLOCK.finditer(text):
            # The block's own body, up to the next top-level resource.
            nxt = BLOCK.search(text, m.end())
            body = text[m.end() : nxt.start() if nxt else len(text)]
            yield tf.name, m.group(1), m.group(2), body


def main() -> int:
    ungated, wrongly_gated, unknown, blocked = [], [], [], []
    gated_addrs: set[str] = set()
    locals_text = _parse_locals("\n".join(f.read_text() for f in sorted(TF.glob("*.tf"))))
    for fname, rtype, rname, body in blocks():
        addr = f"{rtype}.{rname}  ({fname})"
        gated = bool(GATED.search(body))
        key = f"{rtype}.{rname}"
        if gated:
            gated_addrs.add(key)
        if key in ADDRESS_MUST_GATE:
            if not gated:
                ungated.append(addr)
        elif key in ADDRESS_MUST_NOT_GATE:
            if gated:
                wrongly_gated.append(addr)
        elif rtype in MUST_GATE:
            if not gated:
                ungated.append(addr)
        elif rtype in MUST_NOT_GATE:
            if gated:
                wrongly_gated.append(addr)
        else:
            unknown.append(addr)
        if gated:
            blocked += [f"{addr}\n      {why}" for why in destroy_blockers(body, locals_text)]
    emptied = unwrapped_gated_lists(gated_addrs)

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
        (
            "gated but cannot be destroyed",
            blocked,
            "power=off would fail PART WAY THROUGH, after the ALB, the NAT "
            "gateway and the ECS services are already gone",
        ),
        (
            "policy-document list that goes empty under power=off",
            emptied,
            "IAM rejects a statement with no Resource, and the policy is not "
            "gated; wrap it as coalescelist([...], [\"arn:aws:<svc>:::<type>/none\"])",
        ),
    ):
        if items:
            print(f"\n{len(items)} {label}:", file=sys.stderr)
            for a in items:
                print(f"  - {a}", file=sys.stderr)
            print(f"  -> {why}", file=sys.stderr)

    if ungated or wrongly_gated or unknown or blocked or emptied:
        return 1
    total = sum(1 for _ in blocks())
    print(
        f"AWS power flag: {total} resource(s); every hourly-billed one gated, "
        f"every stateful one kept, every gated one destroyable, and no "
        f"policy document left with an empty list."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
