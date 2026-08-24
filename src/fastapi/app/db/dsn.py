"""One place that knows how to build a Postgres DSN.

Before 2026-08-21 there were **sixty** copies of this, one per Hatchet
workflow plus several services — `_dsn()`, `_build_dsn()`, `_pg_dsn()`,
`_dsn_sync()` — each six near-identical lines of `os.environ` reads. They
had already drifted four ways:

  * 21 read ``os.environ.get("POSTGRES_USER", "georag")`` (soft default)
    while 33 read ``os.environ["POSTGRES_USER"]`` (KeyError if unset), so
    the same misconfiguration produced a clean default in one workflow and
    a crash in the next;
  * **two hardcoded ``:5432``**, ignoring ``POSTGRES_DIRECT_PORT``
    entirely — those two connect to the wrong port the moment the direct
    port is not 5432;
  * none of the sixty appended ``sslmode``.

**The sslmode omission is not the security hole it looks like**, and it is
worth writing that down so nobody "fixes" it twice. `georag-pg-cc` has
``require_secure_transport = on``, so the server refuses unencrypted
connections and asyncpg's default ``prefer`` negotiates TLS and cannot fall
back to plaintext. And libpq/asyncpg ``sslmode=require`` does **not** verify
the server certificate either — only ``verify-ca`` / ``verify-full`` do — so
the sixty hand-rolled DSNs were cryptographically identical to the one
"correct" DSN in `main.py` against this server. Nothing was exposed.

What the duplication actually costs is change: every future connection-level
setting — ``statement_cache_size=0`` for PgBouncer, ``application_name`` for
pg_stat_activity attribution, ``connect_timeout``, a Hyperdrive DSN — has to
be made sixty times, and the one that gets missed fails in production at
03:00 during a cron rather than in CI.

Two DSNs exist and the distinction is load-bearing:

``direct=True`` (the default here)
    Straight to Postgres on ``POSTGRES_DIRECT_HOST``/``POSTGRES_DIRECT_PORT``.
    Background work uses this because PgBouncer runs in **transaction**
    pooling mode, where session-scoped state — ``SET`` without ``LOCAL``,
    advisory locks, prepared statements, ``LISTEN`` — does not survive
    between statements. Long-running jobs that set a session GUC must not
    go through the pooler.

``direct=False``
    Through PgBouncer on ``POSTGRES_HOST``/``POSTGRES_PORT``. The FastAPI
    request path uses this: short transactions, high connection churn,
    exactly what a transaction pooler is for.
"""

from __future__ import annotations

import os
from urllib.parse import quote

__all__ = ["build_dsn"]


def _settings() -> object | None:
    """`app.config.settings`, or None when it cannot be constructed.

    Imported lazily and defensively. Settings raises ValidationError when
    required variables are absent, and a DSN builder must not be the thing
    that turns a missing env var into an import-time crash in a standalone
    script.
    """
    try:
        from app.config import settings  # noqa: PLC0415
    except Exception:  # noqa: BLE001 — any import/validation failure
        return None
    return settings


def build_dsn(
    *,
    direct: bool = True,
    scheme: str = "postgres",
    include_sslmode: bool = False,
) -> str:
    """Build a Postgres DSN from the environment.

    ``os.environ`` first, ``app.config.settings`` as a fallback — in that
    order, deliberately.

    Settings-first was tried and reverted on 2026-08-21. ``settings`` is a
    module-level singleton built once at import of ``app.config``, so
    reading it first makes this function ignore any later change to the
    environment. That is a silent semantic change for all sixty call sites
    this replaced, every one of which read ``os.environ`` live, and it broke
    a real test that monkeypatches ``POSTGRES_DIRECT_PORT`` to prove the
    direct port is honoured. In a container the two agree and the order is
    academic; where they disagree, the environment is the thing an operator
    actually set.

    Settings still fills in behind it, which is a small gain over the old
    copies: a value present only in ``.env`` is now picked up instead of
    silently falling through to the hardcoded default.

    Args:
        direct: True (default) for a direct-to-Postgres DSN, bypassing
            PgBouncer. See the module docstring — background work needs
            this because the pooler is in transaction mode.
        scheme: ``postgres`` (what all sixty call sites used) or
            ``postgresql``. asyncpg accepts both.
        include_sslmode: append ``?sslmode=`` from ``POSTGRES_SSLMODE``.
            Off by default because it changes nothing against a server
            with ``require_secure_transport = on`` and would be a
            behaviour change dressed as a cleanup. Turn it on deliberately
            if a deployment ever needs ``verify-full``.

    Returns:
        A DSN string. The password is percent-encoded, which the sixty
        hand-rolled copies did not do — a password containing ``@``, ``/``
        or ``:`` produced a DSN that parsed to the wrong host.
    """
    settings = _settings()

    def _read(name: str, fallback: str) -> str:
        # An empty string counts as UNSET, not as an explicit empty value.
        # Windows in particular treats `SetEnvironmentVariable(name, "")` as
        # a delete, so "" and absent are not reliably distinguishable, and a
        # DSN assembled from an accidentally-blank variable fails in a much
        # more confusing way than one that fell back to a default.
        value = os.environ.get(name)
        if value is not None and value != "":
            return value
        if settings is not None:
            attr = getattr(settings, name, None)
            if attr is not None and str(attr) != "":
                return str(attr)
        return fallback

    user = _read("POSTGRES_USER", "georag")
    password = _read("POSTGRES_PASSWORD", "")
    database = _read("POSTGRES_DB", "georag")

    direct_host = _read("POSTGRES_DIRECT_HOST", "postgresql")
    direct_port = _read("POSTGRES_DIRECT_PORT", "5432")

    if direct:
        host, port = direct_host, direct_port
    else:
        # The pooled fallback is the DIRECT host, not the literal
        # "pgbouncer". That name is a docker-compose service and resolves
        # nowhere else, so as a default it is a hostname that works in one
        # environment and fails DNS in every other -- the same shape as the
        # MV-refresh compose hostname the full-stack review found.
        #
        # Nothing is broken by it today: `direct` defaults to True, so the
        # only caller that reaches this branch is main.py's request-path
        # pool, and fastapi-cc does define POSTGRES_HOST. This is a latent
        # edge removed, not an outage fixed. The next service that calls
        # build_dsn(direct=False) is the one that would have found it.
        #
        # Falling back to the direct host means a deployment naming one
        # Postgres endpoint works, and PgBouncer becomes an explicit
        # opt-in rather than a default that resolves differently per
        # environment.
        #
        # Opting in is NOT currently safe, for a reason worth stating here
        # rather than rediscovering: Azure's built-in PgBouncer runs
        # pool_mode=transaction, and six call sites still use
        # `set_config(..., false)` -- a SESSION-scoped GUC. Under
        # transaction pooling a session GUC does not reliably survive to
        # the next statement and can be observed by whoever holds that
        # server connection next. For app.workspace_id that is a
        # cross-tenant read. Convert those six to transaction scope before
        # pointing POSTGRES_HOST at :6432.
        host = _read("POSTGRES_HOST", direct_host)
        port = _read("POSTGRES_PORT", direct_port)

    # `safe=""` so every reserved character is encoded. An unencoded "@" in
    # a password splits the authority section and the DSN silently points
    # at a different host.
    dsn = (
        f"{scheme}://{quote(user, safe='')}:{quote(password, safe='')}"
        f"@{host}:{port}/{database}"
    )

    if include_sslmode:
        sslmode = _read("POSTGRES_SSLMODE", "").strip()
        if sslmode:
            dsn = f"{dsn}?sslmode={sslmode}"

    return dsn


def redact_dsn(dsn: str) -> str:
    """A DSN safe to log: same string, password replaced by ``*****``.

    Added 2026-08-22 after CodeQL flagged run_golden_benchmark.py for
    clear-text logging. That call site redacted by hand::

        _dsn().replace(os.environ.get("POSTGRES_PASSWORD", "_") or "_", "*****")

    which is only a redaction when POSTGRES_PASSWORD is both set and is the
    password actually in the DSN. `build_dsn` also honours a full
    ``DATABASE_URL``/``POSTGRES_DSN`` override, and in that case the string
    replacement matches nothing and the benchmark logs a live credential at
    INFO into a log store several people can read. A no-op redaction is
    worse than none, because it reads as handled.

    Redact structurally instead: split the URL and drop the password
    component, so it works no matter where the DSN came from. Keeps the
    username, which is what makes the line useful for debugging.

    Anything that does not parse as a URL is returned as ``*****`` rather
    than passed through — an unparseable DSN is exactly the case where a
    stray credential would survive a regex.
    """
    from urllib.parse import urlsplit, urlunsplit

    try:
        parts = urlsplit(dsn)
        password = parts.password
        port = parts.port
    except ValueError:
        # urlsplit parses the netloc lazily, so a malformed authority --
        # ":abc" as a port is the realistic one, from a hand-edited
        # DATABASE_URL -- raises when a component is READ, not when the URL
        # is split. Reading `.port` outside this block meant the one
        # function whose job is to keep a DSN out of a traceback was the
        # thing that put it in one: main.py redacts during lifespan, so the
        # process died at startup with the raw credential in the log, and
        # in the frame locals Sentry ships with the event.
        return "*****"

    if not parts.scheme or password is None:
        # Either not a URL at all (libpq keyword/value form, e.g.
        # "host=... password=..."), or a URL carrying no password. The
        # keyword/value form is not something build_dsn emits, but this
        # helper is the one people will reach for, so do not hand back a
        # string that might contain "password=hunter2".
        return dsn if parts.scheme and "password" not in dsn.lower() else "*****"

    host = parts.hostname or ""
    if port:
        host = f"{host}:{port}"
    netloc = f"{parts.username or ''}:*****@{host}"

    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
