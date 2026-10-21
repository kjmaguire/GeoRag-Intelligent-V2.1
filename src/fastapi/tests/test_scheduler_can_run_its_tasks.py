"""EventBridge Scheduler's role must be able to RunTask the revision it resolves to.

The three schedules (shutdown, startup, token check) name each task family
without a revision so they always launch the latest one. ECS authorises
RunTask against the revisioned ARN that resolves to
(``task-definition/<family>:<n>``), and an IAM resource is a literal match
unless it carries a wildcard, so a bare family ARN denies every launch. That
was the policy until 2026-10-21: no sweep ever started, nothing was logged,
and the platform ran through every nightly window.
"""

from __future__ import annotations

import re
from pathlib import Path

IAM_TF = Path(__file__).resolve().parents[3] / "deploy" / "aws" / "terraform" / "iam.tf"


def _scheduler_policy() -> str:
    text = IAM_TF.read_text(encoding="utf-8")
    block = re.search(r'data\s+"aws_iam_policy_document"\s+"scheduler"\s*\{(.*?)\n\}', text, re.S)
    assert block, "no aws_iam_policy_document.scheduler in iam.tf"
    return block.group(1)


def test_runtask_resources_cover_every_revision() -> None:
    policy = _scheduler_policy()
    assert '"ecs:RunTask"' in policy
    assert re.search(r'"\$\{arn\}:\*"', policy), (
        "the scheduler role's ecs:RunTask resources no longer add `:*` to each "
        "family; RunTask is authorised against the revisioned ARN, so the "
        "schedules' launches are denied and the sweeps never run"
    )


def test_every_scheduled_family_is_in_the_runtask_list() -> None:
    policy = _scheduler_policy()
    for family in ("shutdown_sweep", "startup_sweep", "token_check"):
        assert f"aws_ecs_task_definition.{family})" in policy, (
            f"aws_ecs_task_definition.{family} is scheduled in scheduler.tf but "
            "missing from the scheduler role's ecs:RunTask resources"
        )
