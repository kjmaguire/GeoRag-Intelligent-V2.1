"""The Laravel image must run PHP >= 8.5.12 and keep the PHP2Shell stopgap.

CVE-2026-109618 and CVE-2026-109619 (2026-10-21) are unauthenticated RCEs
fixed in PHP 8.5.12. Before an 8.5.12 image existed, docker/laravel.Dockerfile
turned off the two features the published exploit paths go through; both
`FROM` lines are now pinned to 8.5.12 and the settings stay as a second layer
until production is confirmed on it. The settings only help in the RUNTIME
stage, which is what Octane, Horizon, Reverb and the migrate task run;
written in the builder stage they would never ship.
"""

from __future__ import annotations

import re
from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parents[3] / "docker" / "laravel.Dockerfile"


def _runtime_stage() -> str:
    text = DOCKERFILE.read_text(encoding="utf-8")
    match = re.search(r"^FROM \S+ AS runtime$(.*)", text, re.M | re.S)
    assert match, "docker/laravel.Dockerfile has no `FROM ... AS runtime` stage"
    return match.group(1)


def test_runtime_stage_disables_both_php2shell_paths() -> None:
    runtime = _runtime_stage()
    for setting in ("session.upload_progress.enabled=0", "fatal_error_backtraces=0"):
        assert f'echo "{setting}"' in runtime, (
            f"the runtime stage no longer writes `{setting}`; it is the PHP2Shell "
            "stopgap; remove it deliberately, once production is confirmed on 8.5.12"
        )


def test_mitigation_lands_in_php_conf_d() -> None:
    # PHP reads /usr/local/etc/php/conf.d/*.ini at startup; a file written
    # anywhere else is silently ignored and the image ships unmitigated.
    assert "> /usr/local/etc/php/conf.d/php2shell-mitigation.ini" in _runtime_stage()


def test_both_stages_pin_a_patched_php() -> None:
    # The builder compiles Swoole and phpredis against its PHP; the runtime
    # stage must be the same image or the extensions load against a
    # different ABI. And both must be past the PHP2Shell fix.
    text = DOCKERFILE.read_text(encoding="utf-8")
    pins = re.findall(r"^FROM php:(\d+)\.(\d+)\.(\d+)-cli@(sha256:[0-9a-f]{64}) AS (\w+)$", text, re.M)
    assert {stage for *_, stage in pins} == {"builder", "runtime"}, (
        "both PHP stages must pin `php:<major>.<minor>.<patch>-cli@sha256:...`"
    )
    assert len({digest for *_, digest, _ in pins}) == 1, "builder and runtime must use the same digest"
    for major, minor, patch, _, stage in pins:
        assert (int(major), int(minor), int(patch)) >= (8, 5, 12), (
            f"the {stage} stage pins PHP {major}.{minor}.{patch}, below 8.5.12 (PHP2Shell)"
        )
