"""Sixty hand-rolled DSN builders, now one.

Every Hatchet workflow and several services defined their own `_dsn()` /
`_build_dsn()` / `_pg_dsn()` / `_dsn_sync()` — six near-identical lines of
`os.environ` reads, sixty times over, plus three more assembled inline. They
had already drifted:

  * 21 used ``os.environ.get("POSTGRES_USER", "georag")`` while 33 used
    ``os.environ["POSTGRES_USER"]``, so the same missing variable gave a
    clean default in one workflow and a KeyError in the next;
  * two hardcoded ``:5432`` and ignored ``POSTGRES_DIRECT_PORT`` entirely
    (verbalize_page_images.py, passage_embedder.py);
  * none percent-encoded the password;
  * none appended ``sslmode``.

The sslmode omission was NOT the security hole it looked like — see
`app/db/dsn.py`. The cost was change: any future connection-level setting
had to be made sixty times, and the one that got missed would fail at 03:00
during a cron rather than in CI.
"""

from __future__ import annotations

import pytest

from app.db.dsn import build_dsn

_BASE = {
    "POSTGRES_USER": "georag",
    "POSTGRES_PASSWORD": "secret",
    "POSTGRES_DB": "georag",
    "POSTGRES_HOST": "pgbouncer",
    "POSTGRES_PORT": "6432",
    "POSTGRES_DIRECT_HOST": "pg-direct",
    "POSTGRES_DIRECT_PORT": "5433",
    "POSTGRES_SSLMODE": "require",
}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):
    for key, value in _BASE.items():
        monkeypatch.setenv(key, value)
    return monkeypatch


class TestHostSelection:
    def test_direct_bypasses_the_pooler(self, env) -> None:
        """Background work must not go through PgBouncer.

        The pooler runs in TRANSACTION mode, where session-scoped state —
        SET without LOCAL, advisory locks, prepared statements, LISTEN —
        does not survive between statements.
        """
        assert "@pg-direct:5433/georag" in build_dsn()

    def test_pooled_uses_the_pooler(self, env) -> None:
        assert "@pgbouncer:6432/georag" in build_dsn(direct=False)

    def test_direct_is_the_default(self, env) -> None:
        assert build_dsn() == build_dsn(direct=True)

    def test_the_direct_port_is_honoured(self, env) -> None:
        """Two of the sixty hardcoded :5432 and ignored this variable."""
        env.setenv("POSTGRES_DIRECT_PORT", "5544")

        assert "@pg-direct:5544/" in build_dsn()


class TestCredentials:
    def test_a_password_with_reserved_characters_is_encoded(self, env) -> None:
        """None of the sixty did this.

        An unencoded "@" splits the authority section, so the DSN silently
        points at a different host.
        """
        env.setenv("POSTGRES_PASSWORD", "p@ss/w:rd?x#y")
        dsn = build_dsn()

        assert "p%40ss%2Fw%3Ard%3Fx%23y" in dsn
        assert dsn.count("@") == 1
        assert dsn.endswith("/georag")

    def test_a_user_with_reserved_characters_is_encoded(self, env) -> None:
        env.setenv("POSTGRES_USER", "admin@tenant")

        assert "admin%40tenant" in build_dsn()

    def test_a_missing_password_does_not_raise(self, env) -> None:
        """33 of the sixty used os.environ[...] and raised KeyError here.

        The value that lands is whatever Settings supplies (it may have
        read `.env`), or empty if nothing does — the contract is that
        building a DSN never raises, not that the password is blank.
        """
        env.delenv("POSTGRES_PASSWORD", raising=False)
        dsn = build_dsn()

        assert dsn.startswith("postgres://georag:")
        assert dsn.count("@") == 1


class TestSslmode:
    def test_omitted_by_default(self, env) -> None:
        """Off unless asked for — appending it would be a behaviour change
        dressed as a cleanup, and it buys nothing against a server with
        require_secure_transport=on."""
        assert "sslmode" not in build_dsn()

    def test_appended_when_requested(self, env) -> None:
        assert build_dsn(include_sslmode=True).endswith("?sslmode=require")

    def test_an_empty_sslmode_falls_back_rather_than_emitting_a_blank(
        self, env,
    ) -> None:
        """"" counts as UNSET, not as an explicit empty value.

        Windows treats setting a variable to "" as a delete, so "" and
        absent are not reliably distinguishable. What must never happen is
        a trailing `?sslmode=` with nothing after it, which asyncpg rejects.
        """
        env.setenv("POSTGRES_SSLMODE", "")
        dsn = build_dsn(include_sslmode=True)

        assert not dsn.endswith("?sslmode=")
        assert "?sslmode=" not in dsn or dsn.split("?sslmode=")[1].strip()


class TestScheme:
    def test_defaults_to_the_scheme_all_sixty_used(self, env) -> None:
        assert build_dsn().startswith("postgres://")

    def test_postgresql_is_available_for_the_request_path(self, env) -> None:
        assert build_dsn(scheme="postgresql").startswith("postgresql://")


class TestPrecedence:
    def test_the_environment_wins_over_settings(self, env) -> None:
        """Settings-first was tried and reverted.

        `settings` is a singleton built once at import of app.config, so
        reading it first makes this function ignore later changes to the
        environment — a silent semantic change for all sixty call sites,
        every one of which read os.environ live.
        """
        env.setenv("POSTGRES_DIRECT_HOST", "changed-at-runtime")

        assert "@changed-at-runtime:" in build_dsn()

    def test_defaults_apply_when_nothing_is_set(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        for key in _BASE:
            monkeypatch.delenv(key, raising=False)
        # settings may still supply values; assert only on shape.
        dsn = build_dsn()

        assert dsn.startswith("postgres://")
        assert dsn.count("@") == 1
        assert dsn.count("://") == 1


class TestNoBuildersGrewBack:
    """The CI gate is scripts/ci/dsn_single_source_check.sh; this is the
    same assertion in the fast suite, so it fails locally too."""

    def test_no_hand_rolled_builders_remain(self) -> None:
        import re
        from pathlib import Path

        app_root = Path(__file__).resolve().parent.parent / "app"
        pattern = re.compile(
            r"^\s*def (_dsn|_build_dsn|_pg_dsn|_dsn_sync|_make_dsn)\s*\(",
            re.MULTILINE,
        )

        offenders = [
            str(path.relative_to(app_root))
            for path in app_root.rglob("*.py")
            if "__pycache__" not in path.parts
            and pattern.search(path.read_text(encoding="utf-8"))
        ]

        assert offenders == [], (
            "import build_dsn from app.db.dsn instead of defining a new one"
        )

    def test_no_inline_dsn_fstrings_remain(self) -> None:
        import re
        from pathlib import Path

        app_root = Path(__file__).resolve().parent.parent / "app"
        pattern = re.compile(r'f"postgres(ql)?://')

        offenders = [
            str(path.relative_to(app_root))
            for path in app_root.rglob("*.py")
            if "__pycache__" not in path.parts
            and path.name != "dsn.py"
            and pattern.search(path.read_text(encoding="utf-8"))
        ]

        assert offenders == []


class TestRedactDsn:
    """`redact_dsn` is the only safe way to put a DSN in a log line.

    Added after CodeQL flagged run_golden_benchmark.py, which redacted by
    string-replacing os.environ["POSTGRES_PASSWORD"] out of the DSN. That
    is a no-op whenever the DSN came from a DATABASE_URL override, and a
    no-op redaction is worse than none because it reads as handled.
    """

    def test_the_password_is_replaced_wherever_the_dsn_came_from(self) -> None:
        from app.db.dsn import redact_dsn

        out = redact_dsn(
            "postgresql://georag:s3cret@pg-cc.postgres.database.azure.com"
            ":5432/georag?sslmode=require"
        )

        assert "s3cret" not in out
        # Everything else survives -- a redacted line still has to be
        # useful enough that nobody reaches for the unredacted one.
        assert out == (
            "postgresql://georag:*****@pg-cc.postgres.database.azure.com"
            ":5432/georag?sslmode=require"
        )

    def test_a_dsn_with_no_password_is_unchanged(self) -> None:
        from app.db.dsn import redact_dsn

        dsn = "postgresql://georag@localhost:5432/georag"

        assert redact_dsn(dsn) == dsn

    def test_the_libpq_keyword_form_is_not_passed_through(self) -> None:
        from app.db.dsn import redact_dsn

        # build_dsn never emits this form, but this helper is the one
        # people will reach for. Returning it unchanged would leak.
        assert redact_dsn("host=pg password=s3cret dbname=georag") == "*****"

    def test_the_real_dsn_this_deployment_builds_carries_no_password(self) -> None:
        import os
        from unittest import mock

        from app.db.dsn import build_dsn, redact_dsn

        env = {
            "POSTGRES_USER": "georag",
            "POSTGRES_PASSWORD": "s3cret",
            "POSTGRES_HOST": "pg-cc.postgres.database.azure.com",
            "POSTGRES_DB": "georag",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            assert "s3cret" not in redact_dsn(build_dsn())
