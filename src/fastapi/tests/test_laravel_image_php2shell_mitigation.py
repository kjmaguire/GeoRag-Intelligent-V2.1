"""The Laravel image must carry the PHP2Shell stopgap until it runs PHP 8.5.12.

CVE-2026-109618 and CVE-2026-109619 (2026-10-21) are unauthenticated RCEs
fixed in PHP 8.5.12. The image's base digest is PHP 8.5.10 and no 8.5.12
image existed on Docker Hub that day, so docker/laravel.Dockerfile turns off
the two features the published exploit paths go through. The settings only
help in the RUNTIME stage, which is what Octane, Horizon, Reverb and the
migrate task run; written in the builder stage they would never ship.
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
            "stopgap and may only go once both FROM lines are on PHP >= 8.5.12"
        )


def test_mitigation_lands_in_php_conf_d() -> None:
    # PHP reads /usr/local/etc/php/conf.d/*.ini at startup; a file written
    # anywhere else is silently ignored and the image ships unmitigated.
    assert "> /usr/local/etc/php/conf.d/php2shell-mitigation.ini" in _runtime_stage()
