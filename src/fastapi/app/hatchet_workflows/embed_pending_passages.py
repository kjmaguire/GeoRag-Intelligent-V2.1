"""embed_pending_passages Hatchet workflow (§04i Layer 5 enablement).

Doc-phase 183 — Phase E.1 Track 3.

Wraps `app.services.ingest.passage_embedder.embed_pending_passages` as
a Hatchet workflow so passage embeddings land in Qdrant on a schedule
or post-cluster-ingest trigger.

Manual invocation:
  embed_pending_passages_wf.run({"workspace_id": "<uuid>", "project_id": "<uuid>"})

Cron-fire (when project_id="*"): walks all projects with un-embedded
passages and syncs them. Cron schedule omitted for now — operator
triggers manually after each cluster ingest.
"""
from __future__ import annotations

import contextlib
import logging

import asyncpg
from hatchet_sdk import (
    ConcurrencyExpression,
    ConcurrencyLimitStrategy,
    Context,
)
from pydantic import BaseModel, Field, model_validator

from app.db.dsn import build_dsn
from app.hatchet_workflows import hatchet
from app.services.ingest.passage_embedder import embed_pending_passages

log = logging.getLogger("georag.hatchet.embed_pending_passages")


# F3 (2026-08-11) — keep in lockstep with the eligibility predicate in
# app/services/ingest/passage_embedder.py (embed_pending_passages SELECT):
# passages with ocr_status 'rejected'/'pending_reocr' are never embedded, so
# treating them as "still pending" in the completion sweep would leave runs
# at embed_verify/embedding forever.
_EMBEDDABLE_OCR_PREDICATE = (
    "(p.ocr_status IS NULL OR p.ocr_status NOT IN ('rejected', 'pending_reocr'))"
)


class EmbedPendingPassagesInput(BaseModel):
    # REC#1 (2026-06-03) — a dispatcher must not be able to omit
    # workspace_id and silently scope to the default tenant. Bootstrap
    # callers (manual reingest CLI, backfills) go through
    # `bootstrap_workspace_id(reason=...)` in
    # `app.hatchet_workflows._workspace_input`, which logs + increments
    # WORKSPACE_RESOLUTION_FAILURES so the bootstrap usage stays observable.
    #
    # 2026-08-21 — the field-level `Field(...)` requirement moved to the
    # model validator below. A required field cannot be expressed as a cron
    # payload: a declarative `on_crons` trigger sends NO input (the SDK
    # hardcodes `cron_input=None`), so `{}` is all the validator ever sees
    # and every */10 tick would die on ValidationError.
    #
    # REC#1's actual guarantee — a dispatcher cannot omit workspace_id and
    # silently get the default tenant — is unchanged, and still enforced at
    # construction time rather than deferred to the task body. See
    # _require_workspace_unless_fanout.
    workspace_id: str = Field(
        default="",
        description=(
            "Workspace UUID for RLS scoping. Required for a single-project "
            'run; may be empty ONLY on the project_id="*" fan-out, which '
            "resolves the workspace per project. See "
            "_workspace_input.bootstrap_workspace_id for the legitimate "
            "default-tenant bootstrap path."
        ),
    )
    project_id: str = Field(
        default="*",
        description="Project UUID to embed, or '*' to walk every project with "
                    "un-embedded passages.",
    )
    # 2026-08-11: 32 → 64. Cohere Embed's v2 API takes up to 96 texts per
    # request; bigger batches halve the HTTP round-trips and Qdrant
    # upserts per document. SPLADE memory is unaffected (per-text loop).
    batch_size: int = Field(default=64)
    max_passages: int | None = Field(
        default=None,
        description="Cap for smoke runs. None = no limit.",
    )

    @model_validator(mode="after")
    def _require_workspace_unless_fanout(self) -> EmbedPendingPassagesInput:
        """REC#1, expressed as a rule instead of a required field.

        The hazard REC#1 removed is a dispatcher omitting workspace_id and
        silently scoping to the default tenant. That is still rejected here,
        at construction time, for every dispatcher: naming a specific project
        without a workspace raises.

        The one payload that may omit it is the fan-out, `project_id="*"`,
        which resolves the workspace per project from the passage rows and so
        never scopes to a default. That is also the only shape a cron can
        send, since `on_crons` carries no input at all.
        """
        if self.project_id != "*" and not self.workspace_id:
            raise ValueError(
                "workspace_id is required when project_id names a specific "
                'project; it may only be omitted on the project_id="*" '
                "fan-out, which resolves the workspace per project. For a "
                "legitimate default-tenant caller use "
                "_workspace_input.bootstrap_workspace_id(reason=...)."
            )
        return self


class EmbedPendingPassagesOutput(BaseModel):
    projects_processed: int
    total_seen: int = 0
    total_embedded: int = 0
    total_upserted: int = 0
    total_skipped: int = 0
    errors: list[str] = Field(default_factory=list)
    # Phase 3 reliability spec — count of recovery ingest_progress rows
    # created on this sweep. Exposed for nightly integrity reports.
    recovery_runs_created: int = 0


# One DSN builder for the whole service — see app/db/dsn.py for why
# sixty copies of this existed and what the drift cost.
_dsn = build_dsn


embed_pending_passages_wf = hatchet.workflow(
    name="embed_pending_passages",
    # Doc-phase 183 — daily embed sync at 05:45 UTC (after kg_sync at
    # 05:30).
    # 2026-05-22 — added an "every 10 minutes" safety-net cron so that
    # when the persist-side inline trigger races with a Hatchet retry
    # (BattleNorth bug), unembedded passages get picked up within ~10 min
    # instead of waiting a full day. The function is idempotent (passages
    # already with embedding_id get skipped) so frequent runs are cheap
    # when nothing is pending.
    on_crons=["45 5 * * *", "*/10 * * * *"],
    input_validator=EmbedPendingPassagesInput,
    # Per-workspace singleton. The every-10-min safety-net cron + daily
    # cron + manual triggers all queue behind the in-flight run for the
    # same workspace; different workspaces still embed in parallel.
    # GROUP_ROUND_ROBIN queues rather than cancels so an in-flight large
    # bulk run can't be interrupted by a tiny safety-net tick.
    # Concurrency key: use workspace_id when provided (manual/ingest triggers),
    # fall back to the literal string "cron" for cron-fired runs that have no
    # workspace_id in the input.
    #
    # 2026-08-21 — the 2026-06-01 version of this fallback did not work, and
    # that is why both crons here had effectively never fired: 93 runs over
    # 18 days against ~2,610 expected, every one of them an inline dispatch
    # from ingest_pdf.persist rather than a cron tick.
    #
    # It tested `input.workspace_id != ''`, i.e. for the key being EMPTY. On
    # a cron run the key is ABSENT — a declarative `on_crons` trigger sends
    # no input at all (hatchet_sdk/runnables/workflow.py:257 hardcodes
    # `cron_input=None`), so cel-go raises `no such key: workspace_id` while
    # evaluating the expression, and pkg/repository/task.go:2103 records the
    # run with an initial state of FAILED. The run is failed by the engine
    # before any worker sees it, which is why the worker log had nothing in
    # it to find. `has()` is the absence-safe form; the engine's own tests
    # cover this exact shape (internal/cel/cel_test.go:40), so it compiles
    # at registration — important, since an uncompilable expression fails
    # PutWorkflow and takes the whole worker's registration down. `string()`
    # coerces rather than fails if a caller ever sends a non-string
    # workspace_id (task.go:2108 rejects a non-string concurrency key).
    concurrency=ConcurrencyExpression(
        expression=(
            "has(input.workspace_id) && string(input.workspace_id) != '' "
            "? string(input.workspace_id) : 'cron'"
        ),
        max_runs=1,
        limit_strategy=ConcurrencyLimitStrategy.GROUP_ROUND_ROBIN,
    ),
)


#: Log marker for a Qdrant collection that has silently lost points.
#:
#: PG says a passage is embedded; Qdrant does not have it. Nothing else
#: notices — retrieval just returns fewer hits, and the passage is
#: unreachable while every record says it is fine.
#:
#: Matched by alert rule 5e in deploy/azure/alerts/create-alerts.sh. There
#: is no metric to threshold: the Prometheus registry on this worker is
#: unscraped, so the log line IS the signal.
QDRANT_PARTIAL_LOSS_MARKER = "QDRANT_PARTIAL_LOSS"


#: Hard cap on the bootstrap subprocess spawned by the drift self-heal.
#:
#: init_qdrant.py talks to the same Qdrant instance whose emptiness
#: triggered the heal, so "reachable but not serving" hangs it. Two
#: minutes is far longer than a healthy bootstrap and far shorter than
#: the task's 2 h execution_timeout, which is what it blocked for before.
_QDRANT_BOOTSTRAP_TIMEOUT_S = 120

#: How many passages one drift reset may un-embed.
#:
#: The reset is triggered by a single count() returning zero, which is
#: also what a slow restore looks like. Uncapped it nulled every
#: embedding_id in every workspace and billed a full corpus re-embed on
#: a transient reading. Capped, a false positive costs this many
#: re-embeds and a genuine wipe still recovers completely — the drift
#: condition holds until the collection refills, so the next sweep takes
#: the next batch.
_QDRANT_DRIFT_RESET_BATCH = 5000


@embed_pending_passages_wf.task(execution_timeout="2h", schedule_timeout="2h", retries=0)
async def run(
    input: EmbedPendingPassagesInput, ctx: Context
) -> EmbedPendingPassagesOutput:
    # targets is (workspace_id, project_id). The workspace travels with the
    # project rather than coming from the input, because this fan-out spans
    # every workspace: embedding project P under a different workspace's id
    # binds the wrong RLS scope, so the pass sees no rows and reports a
    # cheerful zero. Latent until now only because the cron never fired.
    if input.project_id == "*":
        conn = await asyncpg.connect(_dsn(), statement_cache_size=0)
        try:
            rows = await conn.fetch(
                "SELECT DISTINCT r.project_id::text AS pid, "
                "       dp.workspace_id::text AS wid "
                "  FROM silver.document_passages dp "
                "  JOIN silver.reports r ON r.report_id = dp.document_id "
                " WHERE dp.embedding_id IS NULL AND r.project_id IS NOT NULL"
            )
            targets = [(r["wid"], r["pid"]) for r in rows if r["wid"]]
        finally:
            await conn.close()
    else:
        # workspace_id is guaranteed non-empty here — the input model's
        # _require_workspace_unless_fanout validator rejects a specific
        # project without one before this task ever runs.
        targets = [(input.workspace_id, input.project_id)]

    project_ids = [pid for _, pid in targets]

    # Phase 3 of the reliability spec — orphan-document recovery layer.
    # Before the per-project embed loop runs, walk silver.document_passages
    # for any document with un-embedded passages older than 5 minutes,
    # take a per-document advisory lock, and create a recovery
    # ingest_progress row linked back to the original via parent_run_id.
    #
    # This gives us observable lineage: every safety-net dispatch is now
    # an auditable attempt with a known parent + reason, not a silent
    # background catch-up.
    try:
        from app.hatchet_workflows import _progress as _ingest_progress
        from app.services.ingest.orphan_sweep import claim_and_record_recovery

        sweep_pool = await _ingest_progress.get_pool()
        claimed, skipped_by_lock = await claim_and_record_recovery(sweep_pool)
        recovery_runs_created = sum(1 for c in claimed if c.recovery_run_id is not None)
        log.info(
            "embed_pending_passages.orphan_sweep claimed=%d (recovery_runs=%d) "
            "skipped_by_lock=%d",
            len(claimed), recovery_runs_created, len(skipped_by_lock),
        )
    except Exception as exc:
        # Recovery-run creation is observability — a failure here must
        # never block the actual embed work below.
        log.warning("embed_pending_passages.orphan_sweep failed: %s", exc)
        recovery_runs_created = 0

    # Phase 6 — publish per-workspace embed-pending gauge. The
    # EmbedPendingPassagesStuck alert fires when any workspace has a
    # non-zero value for 20+ minutes, which catches a stalled embed
    # worker that the orphan sweep failed to recover from.
    try:
        gauge_conn = await asyncpg.connect(_dsn(), statement_cache_size=0)
        try:
            gauge_rows = await gauge_conn.fetch(
                """
                SELECT r.workspace_id::text AS ws, count(*)::int AS n
                FROM silver.document_passages dp
                JOIN silver.reports r ON r.report_id = dp.document_id
                WHERE dp.embedding_id IS NULL
                GROUP BY r.workspace_id
                """,
            )
        finally:
            await gauge_conn.close()
        from app.metrics import EMBED_PENDING_PASSAGES
        for gr in gauge_rows:
            EMBED_PENDING_PASSAGES.labels(workspace_id=gr["ws"]).set(int(gr["n"]))
    except Exception as exc:
        log.debug("embed_pending_passages: gauge publish failed: %s", exc)

    # Qdrant-drift self-healing (2026-08-07). qdrant-cc has no persistent
    # volume: a replica recreation silently wipes every collection while
    # Postgres still says "embedded" — retrieval then returns zero hits until
    # a human notices (bit us live 2026-08-06). On cron sweeps, compare an
    # exact Qdrant point count against PG's embedded-passage count; if the
    # collection is empty/missing while PG believes ≥50 passages are embedded,
    # re-run the bootstrap script (idempotent) and null out embedding_id so
    # this very sweep re-embeds the corpus with no operator involvement.
    # Fails soft: an unreachable Qdrant must never turn into a mass reset.
    if input.project_id == "*":
        try:
            from qdrant_client import AsyncQdrantClient  # noqa: PLC0415

            from app.services.qdrant_conn import qdrant_client_kwargs  # noqa: PLC0415

            _qc = AsyncQdrantClient(**qdrant_client_kwargs())
            try:
                _collections = {
                    c.name for c in (await _qc.get_collections()).collections
                }
                if "georag_chunks" in _collections:
                    _qdrant_points = (
                        await _qc.count("georag_chunks", exact=True)
                    ).count
                else:
                    _qdrant_points = None  # collection itself is gone
            finally:
                await _qc.close()

            _heal_conn = await asyncpg.connect(_dsn(), statement_cache_size=0)
            try:
                _pg_embedded = await _heal_conn.fetchval(
                    "SELECT count(*) FROM silver.document_passages "
                    "WHERE embedding_id IS NOT NULL"
                )
                if (_qdrant_points in (0, None)) and _pg_embedded >= 50:
                    log.error(
                        "embed_pending_passages.qdrant_drift detected: "
                        "qdrant_points=%s pg_embedded=%d — collection wiped. "
                        "Re-bootstrapping and resetting embedding_id for "
                        "automatic re-embed.",
                        _qdrant_points, _pg_embedded,
                    )
                    import asyncio as _aio  # noqa: PLC0415

                    _proc = await _aio.create_subprocess_exec(
                        "python", "/app/scripts/init_qdrant.py",
                        stdout=_aio.subprocess.PIPE,
                        stderr=_aio.subprocess.STDOUT,
                    )
                    # Bounded. The condition that brings us here — Qdrant
                    # answers get_collections but the collection is empty —
                    # is also what a half-up Qdrant looks like during a slow
                    # restore, and init_qdrant.py talks to that same
                    # instance. Unbounded, communicate() blocked for the
                    # task's full 2 h execution_timeout while every inline
                    # embed dispatch from persist queued behind it
                    # (max_runs=1 per workspace).
                    try:
                        _out, _ = await _aio.wait_for(
                            _proc.communicate(),
                            timeout=_QDRANT_BOOTSTRAP_TIMEOUT_S,
                        )
                    except TimeoutError:
                        _proc.kill()
                        with contextlib.suppress(Exception):
                            await _proc.wait()
                        log.error(
                            "embed_pending_passages.qdrant_bootstrap timed out "
                            "after %ds and was killed — NOT resetting "
                            "embedding_id. Qdrant is reachable but not "
                            "serving; this needs a human.",
                            _QDRANT_BOOTSTRAP_TIMEOUT_S,
                        )
                        _out = b""
                    log.info(
                        "embed_pending_passages.qdrant_bootstrap rc=%s tail=%s",
                        _proc.returncode,
                        (_out or b"")[-300:].decode(errors="replace"),
                    )
                    if _proc.returncode == 0:
                        # Capped. This used to be every row in every
                        # workspace in one statement, fired by a single
                        # count() reading zero — so a slow restore or a
                        # collection mid-rebuild nulled the entire corpus
                        # and billed a full re-embed.
                        #
                        # A batch bounds what a false positive costs while
                        # still fully recovering a genuine wipe: the drift
                        # condition stays true until the collection is
                        # repopulated, so successive sweeps keep going. The
                        # ORDER BY makes the batches deterministic instead
                        # of re-picking arbitrary rows each tick.
                        _reset = await _heal_conn.execute(
                            "UPDATE silver.document_passages "
                            "SET embedding_id = NULL, updated_at = NOW() "
                            "WHERE passage_id IN ("
                            "  SELECT passage_id FROM silver.document_passages "
                            "  WHERE embedding_id IS NOT NULL "
                            "  ORDER BY created_at "
                            "  LIMIT $1"
                            ")",
                            _QDRANT_DRIFT_RESET_BATCH,
                        )
                        log.info(
                            "embed_pending_passages.qdrant_drift reset %s "
                            "(cap %d of %d embedded) — re-embed begins this "
                            "sweep; further batches follow on later sweeps "
                            "while the collection stays empty",
                            _reset, _QDRANT_DRIFT_RESET_BATCH, _pg_embedded,
                        )
                        # Re-resolve the target list: the pre-heal query saw
                        # zero pending passages, so without this the reset
                        # rows would wait for the NEXT cron tick.
                        #
                        # Must rebuild `targets`, not `project_ids` — the
                        # embed loop iterates (workspace_id, project_id)
                        # pairs, and project_ids is only a derived label.
                        _rows = await _heal_conn.fetch(
                            "SELECT DISTINCT r.project_id::text AS pid, "
                            "       dp.workspace_id::text AS wid "
                            "  FROM silver.document_passages dp "
                            "  JOIN silver.reports r ON r.report_id = dp.document_id "
                            " WHERE dp.embedding_id IS NULL AND r.project_id IS NOT NULL"
                        )
                        targets = [(r["wid"], r["pid"]) for r in _rows if r["wid"]]
                        project_ids = [pid for _, pid in targets]
                elif _qdrant_points is not None and _pg_embedded > 0:
                    # F21 (2026-08-11) — partial-loss detection. The
                    # all-empty branch above only fires when the collection
                    # is wiped; a PARTIALLY lost collection (replica
                    # recreation mid-upsert, selective delete) previously
                    # surfaced nowhere until the nightly spot-check sampled
                    # into the hole. Compare exact per-project counts
                    # (PG embedded vs Qdrant filtered by project_id) and log
                    # a WARNING on >2% mismatch. Observability only — no
                    # automatic reset, and the existing all-empty self-heal
                    # behaviour is unchanged.
                    try:
                        from qdrant_client import models as _qmodels  # noqa: PLC0415

                        _proj_rows = await _heal_conn.fetch(
                            "SELECT r.project_id::text AS pid, count(*)::int AS n "
                            "  FROM silver.document_passages dp "
                            "  JOIN silver.reports r ON r.report_id = dp.document_id "
                            " WHERE dp.embedding_id IS NOT NULL "
                            "   AND r.project_id IS NOT NULL "
                            " GROUP BY r.project_id"
                        )
                        _qc_cnt = AsyncQdrantClient(**qdrant_client_kwargs())
                        try:
                            for _pr in _proj_rows:
                                _pg_n = int(_pr["n"])
                                _q_n = (await _qc_cnt.count(
                                    "georag_chunks",
                                    count_filter=_qmodels.Filter(must=[
                                        _qmodels.FieldCondition(
                                            key="project_id",
                                            match=_qmodels.MatchValue(value=_pr["pid"]),
                                        ),
                                    ]),
                                    exact=True,
                                )).count
                                if _pg_n > 0 and (_pg_n - _q_n) / _pg_n > 0.02:
                                    # ERROR, and carrying the marker. This
                                    # was a WARNING in a stream nobody
                                    # watches, which for a gap PG cannot
                                    # see is the same as no detection: the
                                    # passages are unretrievable and every
                                    # record says they are fine.
                                    log.error(
                                        "%s project=%s pg_embedded=%d qdrant=%d "
                                        "(%.1f%% missing) — Qdrant dropped points "
                                        "PG still believes are embedded; run "
                                        "scripts/reset_embeddings_for_reencode.py "
                                        "for the project or investigate the "
                                        "collection.",
                                        QDRANT_PARTIAL_LOSS_MARKER,
                                        _pr["pid"], _pg_n, _q_n,
                                        100.0 * (_pg_n - _q_n) / _pg_n,
                                    )
                        finally:
                            await _qc_cnt.close()
                    except Exception as _plexc:
                        log.debug(
                            "embed_pending_passages.qdrant_partial_loss check "
                            "failed (soft): %s", _plexc,
                        )
            finally:
                await _heal_conn.close()
        except Exception as exc:
            log.warning(
                "embed_pending_passages.qdrant_drift check failed (soft): %s", exc,
            )

    log.info("embed_pending_passages.start projects=%d", len(project_ids))

    total_seen = 0
    total_embedded = 0
    total_upserted = 0
    total_skipped = 0
    errors: list[str] = []

    # F28 (2026-08-11) — construct the embedding model ONCE per sweep and
    # pass it into every per-project call. Previously embed_pending_passages
    # reloaded the model (SentenceTransformer weights / Foundry client) for
    # every project on every 10-minute tick. Load failure falls back to
    # None so each per-project call retries the load itself (old behaviour).
    embedding_model = None
    if project_ids:
        try:
            from app.services.ingest.passage_embedder import load_embedding_model
            embedding_model = load_embedding_model()
        except Exception as exc:
            log.warning(
                "embed_pending_passages.model_preload_failed err=%s — "
                "falling back to per-project loads", exc,
            )

    # Workspaces this run actually wrote points for. The Guard 3 retrieval
    # smoke below needs a real workspace to query, and on the fan-out there
    # is no single input workspace to use.
    upserted_workspaces: set[str] = set()

    for wid, pid in targets:
        try:
            r = await embed_pending_passages(
                workspace_id=wid,
                project_id=pid,
                embedding_model=embedding_model,
                batch_size=input.batch_size,
                max_passages=input.max_passages,
            )
            total_seen += r.passages_seen
            total_embedded += r.passages_embedded
            total_upserted += r.qdrant_points_upserted
            total_skipped += r.passages_skipped
            errors.extend(r.errors)
            if r.qdrant_points_upserted > 0:
                upserted_workspaces.add(wid)
        except Exception as e:
            errors.append(f"project={pid}:{type(e).__name__}:{e}")
            log.warning(
                "embed_pending_passages.project_failed pid=%s err=%s", pid, e,
            )

    # Orphan / cross-project pass: passages without a parent report
    # (chunk_kind in {'public_geo_synthesis','kg_narrative',
    # 'structured_summary',...}) have document_id NULL so the per-project
    # loop above never touches them. Sweep them so the TIER 0b public-geo
    # backfill and ADR-0012 synthesizer outputs get embedded.
    #
    # "Unscoped" was always aspirational — embed_pending_passages binds RLS
    # to whatever workspace_id it is handed, so a single id covers exactly
    # one workspace's orphans. Resolve the owning workspaces from the rows
    # themselves and sweep each, same as the per-project pass above.
    if input.project_id == "*":
        orphan_workspaces: list[str] = []
        try:
            orphan_conn = await asyncpg.connect(_dsn(), statement_cache_size=0)
            try:
                orphan_rows = await orphan_conn.fetch(
                    "SELECT DISTINCT workspace_id::text AS wid "
                    "  FROM silver.document_passages "
                    " WHERE embedding_id IS NULL "
                    "   AND document_id IS NULL "
                    "   AND workspace_id IS NOT NULL"
                )
                orphan_workspaces = [r["wid"] for r in orphan_rows]
            finally:
                await orphan_conn.close()
        except Exception as e:
            errors.append(f"orphan_pass_discovery:{type(e).__name__}:{e}")
            log.warning("embed_pending_passages.orphan_discovery_failed err=%s", e)

        for wid in orphan_workspaces:
            try:
                r = await embed_pending_passages(
                    workspace_id=wid,
                    project_id=None,
                    embedding_model=embedding_model,
                    batch_size=input.batch_size,
                    max_passages=input.max_passages,
                )
                total_seen += r.passages_seen
                total_embedded += r.passages_embedded
                total_upserted += r.qdrant_points_upserted
                total_skipped += r.passages_skipped
                errors.extend(r.errors)
                if r.qdrant_points_upserted > 0:
                    upserted_workspaces.add(wid)
                log.info(
                    "embed_pending_passages.orphan_pass ws=%s seen=%d embedded=%d "
                    "upserted=%d",
                    wid, r.passages_seen, r.passages_embedded,
                    r.qdrant_points_upserted,
                )
            except Exception as e:
                errors.append(f"orphan_pass:{wid}:{type(e).__name__}:{e}")
                log.warning(
                    "embed_pending_passages.orphan_pass_failed ws=%s err=%s", wid, e,
                )

    log.info(
        "embed_pending_passages.complete projects=%d seen=%d embedded=%d "
        "upserted=%d skipped=%d errors=%d",
        len(project_ids), total_seen, total_embedded, total_upserted,
        total_skipped, len(errors),
    )

    # 2026-06-01 Guard 3 — post-embed retrieval smoke.
    # Only runs when this workflow actually upserted points. Picks one
    # freshly-embedded passage, encodes its first words as a query, runs
    # the hybrid_query pipeline against the workspace, and asserts the
    # top hit carries non-empty text. This exercises the FULL retrieval
    # contract that user-facing chat depends on (dense + sparse encoders,
    # Qdrant index, hybrid scoring, payload extraction) — Guard 1 only
    # verifies what we wrote and Guard 2 only verifies payload shape at
    # rest. A failure here means the payload looks right but chat is
    # still broken (e.g., sparse encoder regressed, scoring threshold
    # too tight, payload key renamed without retrieval-side update).
    # Failure is loud (ERROR + Prom + audit) but non-blocking — the
    # data IS embedded, the issue is in the query path and needs human
    # triage rather than blocking ingest.
    #
    # On the fan-out there is no input workspace, so smoke one that this run
    # actually wrote points for — a smoke against an arbitrary or empty
    # workspace proves nothing. Sorted for a deterministic pick.
    smoke_workspace = input.workspace_id or (
        sorted(upserted_workspaces)[0] if upserted_workspaces else ""
    )
    if total_upserted > 0 and smoke_workspace:
        try:
            from app.hatchet_workflows.embed_pending_passages_smoke import (  # noqa: PLC0415
                run_retrieval_smoke,
            )
            smoke = await run_retrieval_smoke(workspace_id=smoke_workspace)
            log.info("embed_pending_passages.smoke result=%s", smoke)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"smoke_failed:{type(exc).__name__}:{exc}")
            log.exception(
                "embed_pending_passages.smoke_failed err=%s — embed succeeded "
                "but retrieval is broken for workspace=%s. Investigate before "
                "the next user query.",
                exc, smoke_workspace,
            )

    # Sweep silver.ingest_progress: any row sitting at embed_verify/embedding
    # for a project that now has zero unembedded passages is logically
    # finished — flip it to 'completed' so the UI bar fills.
    #
    # Two-step (per-run) instead of one big UPDATE so we can:
    #   1. Use the canonical mark_completed_by_run (conditional terminal
    #      update + Prometheus metrics + status='completed' enum write).
    #      The previous one-shot UPDATE only set current_step='completed'
    #      and left status='started', which then got clobbered to
    #      'timed_out' by stale_run_detector 15 minutes later.
    #   2. Emit the per-run Reverb broadcast so IngestionRuns.tsx flips
    #      to "Completed" without waiting for its poll tick.
    try:
        from app.services.laravel_bridge import post_ingestion_progress
        sweep_pool2 = await _ingest_progress.get_pool()
        async with sweep_pool2.acquire() as sweep_conn:
            # F2 (2026-08-11) — scope the "fully embedded?" test to the run's
            # OWN document when the row carries a report_id (stamped by
            # ingest_pdf.persist). Embeds serialize per workspace, so the old
            # project-wide predicate held every run hostage to the slowest
            # document in a bulk import. NULL report_id (recovery rows, rows
            # that died before persist) keeps the project-wide fallback.
            rows_to_complete = await sweep_conn.fetch(
                f"""
                SELECT ip.run_id::text       AS run_id,
                       ip.workspace_id::text AS workspace_id,
                       ip.project_id::text   AS project_id
                FROM silver.ingest_progress ip
                WHERE ip.status NOT IN ({_ingest_progress.TERMINAL_STATUS_SQL})
                  AND ip.current_step IN ('embed_verify', 'embedding')
                  AND ip.project_id::text = ANY($1::text[])
                  AND NOT EXISTS (
                        SELECT 1
                        FROM silver.document_passages p
                        JOIN silver.reports r ON r.report_id = p.document_id
                        WHERE p.embedding_id IS NULL
                          AND {_EMBEDDABLE_OCR_PREDICATE}
                          AND CASE WHEN ip.report_id IS NOT NULL
                                   THEN p.document_id = ip.report_id
                                   ELSE r.project_id = ip.project_id
                              END
                  )
                """,
                project_ids,
            )

        flipped = 0
        for r in rows_to_complete:
            transitioned = await _ingest_progress.mark_completed_by_run(
                run_id=r["run_id"],
            )
            if not transitioned:
                continue
            flipped += 1
            try:
                await post_ingestion_progress(
                    workspace_id=r["workspace_id"],
                    project_id=r["project_id"],
                    run_id=r["run_id"],
                    stage="embedding",
                    status="completed",
                    message="Ingestion complete; all chunks embedded.",
                )
            except Exception as exc:
                log.warning(
                    "embed_pending_passages.completion_broadcast failed "
                    "run=%s err=%s", r["run_id"], exc,
                )
        if flipped:
            log.info(
                "embed_pending_passages: marked %d ingest_progress run(s) "
                "completed via sweep", flipped,
            )
    except Exception as e:
        log.warning("embed_pending_passages: ingest_progress sweep failed: %s", e)

    return EmbedPendingPassagesOutput(
        projects_processed=len(project_ids),
        total_seen=total_seen,
        total_embedded=total_embedded,
        total_upserted=total_upserted,
        total_skipped=total_skipped,
        errors=errors,
        recovery_runs_created=recovery_runs_created,
    )


__all__ = [
    "embed_pending_passages_wf",
    "EmbedPendingPassagesInput",
    "EmbedPendingPassagesOutput",
]
