"""CLI for the §10.2 mechanical golden-questions seeder (doc-phase 124).

Usage:

    docker exec georag-fastapi python -m \\
        app.services.eval.mechanical_questions \\
        --user-id 1 \\
        --commit

Without `--commit`, runs in DRY-RUN mode — reports the diff that
WOULD be applied without writing anything.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

import asyncpg

from app.services.eval.mechanical_questions import (
    ALL_MECHANICAL_QUESTIONS,
    seed_mechanical_questions,
)


logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("mechanical_seeder")


def _dsn() -> str:
    user = os.environ.get("POSTGRES_USER", "georag")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


async def _main_async(args: argparse.Namespace) -> int:
    if not args.commit:
        log.info("\nDRY-RUN — no writes will occur. Pass --commit to apply.\n")
        # Tally what's queued
        from collections import Counter
        counts = Counter(q["question_set"] for q in ALL_MECHANICAL_QUESTIONS)
        log.info("  Mechanical questions queued: %d total", len(ALL_MECHANICAL_QUESTIONS))
        for qset, n in sorted(counts.items()):
            log.info("    %-25s %d", qset, n)
        log.info("")
        log.info("Run with --commit + --user-id <ops_user_id> to seed.")
        return 0

    conn = await asyncpg.connect(_dsn(), statement_cache_size=0)
    try:
        report = await seed_mechanical_questions(
            conn,
            authored_by_user_id=args.user_id,
            questions=ALL_MECHANICAL_QUESTIONS,
        )
        log.info("")
        log.info("  Mechanical questions seeded:")
        log.info("    inserted:    %d", report.inserted)
        log.info("    updated:     %d", report.updated)
        log.info("    unchanged:   %d", report.unchanged)
        log.info("    total:       %d", report.total_processed)
        log.info("")
        return 0
    finally:
        await conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.services.eval.mechanical_questions",
        description="Seed mechanical golden questions into eval.golden_questions.",
    )
    parser.add_argument("--user-id", type=int, default=None,
                        help="public.users.id recorded as author + reviewer")
    parser.add_argument("--commit", action="store_true",
                        help="Apply writes (default: dry-run)")
    args = parser.parse_args()

    if args.commit and args.user_id is None:
        parser.error("--user-id is required when using --commit")

    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    sys.exit(main())
