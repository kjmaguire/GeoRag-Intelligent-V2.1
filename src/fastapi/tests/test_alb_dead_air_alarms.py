"""Every service behind the ALB has a dead-air alarm, and a composite that pages.

variables.tf said from 2026-09-16 that ``HealthyHostCount`` covers both
laravel-octane and laravel-reverb. Only Octane's target group had an alarm on
it, so a dead Reverb -- answers produced, nothing reaching the browser, every
other alarm green -- was silent while a comment claimed otherwise (AW-12,
2026-10-10). Nothing compared the two, because the target groups are in
services.tf and the alarms are in alerts.tf.

Pinned here, by reading both files:

* every ``aws_lb_target_group`` has a ``HealthyHostCount`` alarm on its own
  ``arn_suffix``, so a third service put behind the ALB cannot be added silent;
* every such alarm has a composite that pages, rather than the alarm itself
  carrying the action (it would fire every night by design);
* every composite suppresses with the maintenance-window alarm and with the SAME
  two timings, which come from one local. Two copies of ``2700`` would drift the
  first time one is tuned, and the composite that is wrong pages every morning.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
TERRAFORM = REPO / "deploy" / "aws" / "terraform"

_BLOCK = r'resource\s+"{kind}"\s+"(\w+)"\s*\{{(.*?)\n\}}'


def blocks(text: str, kind: str) -> dict[str, str]:
    """name -> body of every ``resource "<kind>" "<name>"`` block."""
    return {
        name: body
        for name, body in re.findall(_BLOCK.format(kind=kind), text, re.S)
    }


def target_groups(services_tf: str) -> set[str]:
    return set(blocks(services_tf, "aws_lb_target_group"))


def dead_air_alarms(alerts_tf: str) -> dict[str, str]:
    """alarm resource name -> the target group resource name it watches."""
    found: dict[str, str] = {}
    for name, body in blocks(alerts_tf, "aws_cloudwatch_metric_alarm").items():
        if not re.search(r'metric_name\s*=\s*"HealthyHostCount"', body):
            continue
        group = re.search(
            r"TargetGroup\s*=\s*aws_lb_target_group\.(\w+)\[0\]\.arn_suffix", body
        )
        found[name] = group.group(1) if group else "?"
    return found


def problems(services_tf: str, alerts_tf: str) -> list[str]:
    out = []
    alarms = dead_air_alarms(alerts_tf)
    watched = set(alarms.values())

    for group in sorted(target_groups(services_tf) - watched):
        out.append(f"target group {group!r} has no HealthyHostCount alarm")
    for alarm, group in sorted(alarms.items()):
        if group == "?":
            out.append(f"alarm {alarm!r} watches no target group this test can read")

    composites = blocks(alerts_tf, "aws_cloudwatch_composite_alarm")
    for alarm in sorted(alarms):
        paging = [
            name for name, body in composites.items()
            if re.search(
                rf"ALARM\(\$\{{aws_cloudwatch_metric_alarm\.{alarm}\[0\]\.alarm_name\}}\)",
                body,
            )
        ]
        if not paging:
            out.append(f"alarm {alarm!r} has no composite alarm that pages on it")
        for name in paging:
            body = composites[name]
            if not re.search(
                r"alarm\s*=\s*aws_cloudwatch_metric_alarm\.maintenance_window\[0\]\.alarm_name",
                body,
            ):
                out.append(f"composite {name!r} does not suppress on the maintenance window")
            if not re.search(r"wait_period\s*=\s*local\.dead_air_wait_period\b", body):
                out.append(f"composite {name!r} does not take wait_period from local.dead_air_wait_period")
            if not re.search(r"extension_period\s*=\s*local\.dead_air_extension_period\b", body):
                out.append(
                    f"composite {name!r} does not take extension_period from "
                    "local.dead_air_extension_period"
                )
            if re.search(r"^\s*alarm_actions\s*=", alarms_body(alerts_tf, alarm), re.M):
                out.append(f"alarm {alarm!r} carries the action itself, so it pages every night")
    return out


def alarms_body(alerts_tf: str, alarm: str) -> str:
    return blocks(alerts_tf, "aws_cloudwatch_metric_alarm")[alarm]


def test_every_alb_target_group_pages_on_dead_air() -> None:
    found = problems(
        (TERRAFORM / "services.tf").read_text(encoding="utf-8"),
        (TERRAFORM / "alerts.tf").read_text(encoding="utf-8"),
    )
    assert not found, (
        "ALB target groups, their dead-air alarms and the composites that page "
        "on them:\n  " + "\n  ".join(found)
    )


def test_both_known_services_are_covered() -> None:
    """Guards the guard: the scan found the two groups it exists for."""
    groups = target_groups((TERRAFORM / "services.tf").read_text(encoding="utf-8"))
    watched = set(dead_air_alarms((TERRAFORM / "alerts.tf").read_text(encoding="utf-8")).values())
    assert {"octane", "reverb"} <= groups
    assert {"octane", "reverb"} <= watched


def test_the_shared_timings_are_defined_once() -> None:
    text = (TERRAFORM / "alerts.tf").read_text(encoding="utf-8")
    for local in ("dead_air_wait_period", "dead_air_extension_period"):
        assert len(re.findall(rf"^\s{{2}}{local}\s*=\s*\d+\s*$", text, re.M)) == 1, local
    # Not preceded by a word character: `dead_air_wait_period = 900` is the one
    # definition, and ends in the same letters.
    assert not re.search(r"(?<!\w)(wait_period|extension_period)\s*=\s*\d+", text), (
        "a composite sets a suppressor timing as a literal again; use the shared local"
    )


# --- the checker itself --------------------------------------------------

_GROUPS = (
    'resource "aws_lb_target_group" "octane" {\n  port = 80\n}\n'
    'resource "aws_lb_target_group" "reverb" {\n  port = 8080\n}\n'
)


def _alarm(name: str, group: str) -> str:
    return (
        f'resource "aws_cloudwatch_metric_alarm" "{name}" {{\n'
        '  metric_name = "HealthyHostCount"\n'
        f"  dimensions = {{\n    TargetGroup = aws_lb_target_group.{group}[0].arn_suffix\n  }}\n"
        "}\n"
    )


def _composite(name: str, alarm: str, wait: str = "local.dead_air_wait_period") -> str:
    return (
        f'resource "aws_cloudwatch_composite_alarm" "{name}" {{\n'
        f'  alarm_rule = "ALARM(${{aws_cloudwatch_metric_alarm.{alarm}[0].alarm_name}})"\n'
        "  actions_suppressor {\n"
        "    alarm            = aws_cloudwatch_metric_alarm.maintenance_window[0].alarm_name\n"
        f"    wait_period      = {wait}\n"
        "    extension_period = local.dead_air_extension_period\n"
        "  }\n"
        "}\n"
    )


_GOOD = (
    _alarm("octane_dead_air", "octane") + _composite("octane_page", "octane_dead_air")
    + _alarm("reverb_dead_air", "reverb") + _composite("reverb_page", "reverb_dead_air")
)


def test_the_checker_accepts_both_services_covered() -> None:
    assert problems(_GROUPS, _GOOD) == []


def test_the_checker_catches_the_reverb_gap() -> None:
    """The case AW-12 found."""
    only_octane = _alarm("octane_dead_air", "octane") + _composite("octane_page", "octane_dead_air")
    assert problems(_GROUPS, only_octane) == ["target group 'reverb' has no HealthyHostCount alarm"]


def test_the_checker_catches_an_alarm_nothing_pages_on() -> None:
    no_composite = _GOOD.replace(_composite("reverb_page", "reverb_dead_air"), "")
    assert problems(_GROUPS, no_composite) == [
        "alarm 'reverb_dead_air' has no composite alarm that pages on it"
    ]


def test_the_checker_catches_a_literal_timing() -> None:
    drifted = _GOOD.replace(
        _composite("reverb_page", "reverb_dead_air"),
        _composite("reverb_page", "reverb_dead_air", wait="600"),
    )
    assert problems(_GROUPS, drifted) == [
        "composite 'reverb_page' does not take wait_period from local.dead_air_wait_period"
    ]


def test_the_checker_catches_an_alarm_that_pages_by_itself() -> None:
    noisy = _GOOD.replace(
        'resource "aws_cloudwatch_metric_alarm" "reverb_dead_air" {\n',
        'resource "aws_cloudwatch_metric_alarm" "reverb_dead_air" {\n  alarm_actions = local.alarm_actions\n',
    )
    assert problems(_GROUPS, noisy) == [
        "alarm 'reverb_dead_air' carries the action itself, so it pages every night"
    ]
