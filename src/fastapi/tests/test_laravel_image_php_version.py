"""The Laravel image must stay on a PHP release with the PHP2Shell fix.

CVE-2026-109618 and CVE-2026-109619 (2026-10-21) are unauthenticated RCEs
fixed in PHP 8.5.12. docker/laravel.Dockerfile pins both stages to
`php:8.5.12-cli` by digest; the php.ini stopgap that covered the gap before
that image existed was removed once production was confirmed on 8.5.12.
"""

from __future__ import annotations

import re
from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parents[3] / "docker" / "laravel.Dockerfile"


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
