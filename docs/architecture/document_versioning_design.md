# Document versioning and supersession — design

**Status:** Schema drafted (not applied). Supersession detection + Qdrant payload sync NOT implemented.
**Plan reference:** §1h, §3b (multi-document retrieval with authority ranking — versioning is the prerequisite)

## What this closes

Plan §1h: when a client uploads a new NI 43-101 superseding an older one, the old document's chunks remain in Qdrant. The retrieval system has no way to prefer the current resource value over the superseded one.

## Supersession detection rule

On ingestion of a new document, **after classification but before status flips to `ready`**:

```python
candidates = (
    DocumentVersion
    .where(workspace_id == new.workspace_id)
    .where(report_type == new.report_type)
    .where(is_current == True)
    .where(property_id == new.property_id OR property_id IS NULL)
    .order_by(effective_date.desc())
    .all()
)

for old in candidates:
    if old.effective_date is None or new.effective_date is None:
        # No basis for comparison — do not auto-supersede; flag for SME review
        flag_for_sme_review(new, old, reason="missing_effective_date")
        continue
    if new.effective_date > old.effective_date:
        old.is_current = False
        old.superseded_by_id = new.document_id
        old.superseded_at = now()
        old.superseded_by_event = "auto_detected_on_ingest"
        old.supersession_reason = f"newer {new.report_type} effective {new.effective_date}"
        old.save()
        qdrant_mark_chunks_superseded(old.document_id)
```

Three guards:

1. **`property_id IS NULL` match** — when neither the new nor the old document has a property_id (project-level filings without an obvious property), the candidate set widens to all of the report_type's current docs in the workspace. This deliberately *over-includes*; SME review catches false-positive supersessions.
2. **`effective_date` required** — no date on either side → no auto-supersession. The flag-for-sme-review keeps the case visible without making a bad call silently.
3. **Atomic per-old-document supersession** — multiple superseded candidates each get their own row update. If 3 old NI 43-101s exist when the 4th lands, all 3 flip in a single transaction.

## Qdrant payload sync

`qdrant_mark_chunks_superseded(document_id)` walks `silver.document_passages` for the document and issues `client.set_payload(collection_name="georag_reports", payload={"is_current": False, "superseded_at": <iso>}, points=<list of point IDs>)`.

This is a separate write path (Qdrant is not in a DB transaction with Postgres). On Qdrant failure: the Postgres row is committed but a retry job (Hatchet) reconciles within the next sweep window. Idempotent — re-running `set_payload` with the same fields is a no-op.

## Retrieval default filter (plan §1h)

The agentic_retrieval `execute_node` already builds Qdrant filters from the envelope. Add:

```python
must_filters.append(FieldCondition(key="is_current", match=MatchValue(value=True)))
```

Override conditions:

- Explicit historical query envelope flag `include_superseded=True` → drop the filter
- Multi-document synthesis intent (plan §3b) → keep current-only by default, but the `assemble_node` can issue a follow-up "give me what the superseded version said" search if conflict detection fires

## Conflict surfacing format (plan §1h verbatim)

When current and superseded documents contain conflicting values for the same metric, the answer surfaces the conflict:

```
"The current [2024 NI 43-101] states X.
The superseded [2021 NI 43-101] stated Y.
The change reflects [interpretation from current document]."
```

This is handled by the answer-assembler / response template, not at retrieval time. The retrieval layer's job is just to make both available; the assembler decides whether to surface or merge.

## State transitions

```
draft        (row created at upload, no version row yet)
  → versioned    (this migration's row created on classification)
  → current      (is_current=true; default new state)
  → superseded   (is_current=false; superseded_by_id+superseded_at set)
```

The CHECK constraint `document_versions_supersession_consistent` enforces the `is_current` ↔ `superseded_by_id` invariant — you cannot mark `is_current=false` without naming the successor, and you cannot mark `is_current=true` while pointing at a successor.

## Acceptance criteria (plan §1h)

- [x] Schema captures supersession (this migration)
- [ ] Re-uploading a new NI 43-101 marks the old as superseded — DETECTION NOT IMPLEMENTED
- [ ] Qdrant retrieval returns only current chunks by default — FILTER NOT WIRED
- [ ] Historical retrieval works when explicitly requested — `include_superseded` envelope flag NOT ADDED
- [ ] A query for "current resource estimate" returns the 2024 value — depends on the above

## Decisions captured — 2026-05-27 morning

Kyle reviewed and accepted all four recommendations:

| Q | Decision | Implication |
|---|---|---|
| Q9 | **Plain FK** to `silver.reports(report_id)`; reports-only for now | Kyle considered polymorphic `(document_kind, document_id)` but reverted: DB-level FK enforcement is worth keeping. If other doc kinds need versioning later, that's a future migration with a clear path (drop FK, add document_kind column, backfill). |
| Q10 | **Separate extractor doc** for `effective_date` extraction patterns | Per-report-type pattern matching lives in its own design doc when implementation starts. This doc keeps the versioning contract clean. |
| Q11 | **Fuzzy `property_name` match (Levenshtein ≤ 3)** as fallback when `property_id` is unresolved | Implementation: try `property_id` exact match first; if NULL, try `property_name` Levenshtein against `silver.projects.property_name` with threshold ≤ 3; if still no match, flag for SME review rather than auto-supersede. |
| Q12 | **One-shot backfill** creating one `is_current=true` row per existing report; no supersession history reconstructed | A separate seeder (NOT a migration — too much data to put in a migration's `up()`) inserts the version rows when this lands. Idempotent by `UNIQUE (document_id)`. |
