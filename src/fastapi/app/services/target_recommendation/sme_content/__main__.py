"""CLI entry point for the SME content seeder (doc-phase 123).

Usage:

    docker exec georag-fastapi python -m \\
        app.services.target_recommendation.sme_content \\
        --slug athabasca_uranium \\
        --user-id 1 \\
        --activate

The seeder will refuse to run if the content module still has empty
TODO blocks. Edit the module file first, save, then re-run.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

import asyncpg

from app.services.target_recommendation.sme_content.seed_runner import (
    SmeContentNotReadyError,
    seed_deposit_model_from_module,
)

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("sme_seeder")


def _dsn() -> str:
    user = os.environ.get("POSTGRES_USER", "georag")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


async def _main_async(args: argparse.Namespace) -> int:
    module_path = (
        f"app.services.target_recommendation.sme_content.{args.slug}"
    )

    conn = await asyncpg.connect(_dsn(), statement_cache_size=0)
    try:
        try:
            result = await seed_deposit_model_from_module(
                conn,
                module_path=module_path,
                initiated_by_user_id=args.user_id,
                activate_new_version=args.activate,
            )
        except SmeContentNotReadyError as exc:
            log.error("\n%s\n", exc)
            log.error(
                "Fix the blockers in:\n  src/fastapi/%s\n",
                module_path.replace(".", "/") + ".py",
            )
            return 2

        verb = "Created" if result.was_created else "Updated"
        log.info("\n  %s deposit model: %s", verb, result.slug)
        log.info("    target_model_id        : %s", result.target_model_id)
        log.info("    new version number     : %d", result.new_version_number)
        log.info("    new version_id         : %s", result.new_version_id)
        log.info("    is_active              : %s", args.activate)
        if args.activate:
            log.info("    deactivated prior      : %d version(s)",
                     result.deactivated_versions)
        log.info("    audit_ledger_id        : %s", result.audit_ledger_id)
        log.info("")
        return 0
    finally:
        await conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.services.target_recommendation.sme_content",
        description="Seed a deposit model from an SME content module.",
    )
    parser.add_argument(
        "--slug",
        required=True,
        help="Content module slug under sme_content/ (e.g. athabasca_uranium)",
    )
    parser.add_argument(
        "--user-id",
        type=int,
        required=True,
        help="public.users.id of the geologist running the seed "
             "(recorded on the audit ledger)",
    )
    parser.add_argument(
        "--activate",
        action="store_true",
        help="Activate the new version (and deactivate any prior active). "
             "Omit to land the new version inactive for A/B comparison.",
    )
    args = parser.parse_args()
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    sys.exit(main())
