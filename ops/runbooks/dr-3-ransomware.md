# DR Runbook 3 — Ransomware / hostile-actor data tampering (§26.5 scenario 3)

**Status:** Production-shape (doc-phase 185). Upgraded from the
doc-phase 104 skeleton by Phase H3.

The most stringent recovery scenario: an adversary modifies data
in-place, OR a malicious insider drops/alters rows with no upstream
notice. Discovery typically via the **audit hash-chain break** (the
Phase 0 substrate's tamper-evidence trigger) OR via off-cluster
forensics. Recovery MUST come from immutable cold-tier snapshots —
the hot store is contaminated.

---

## Scope

- **In scope:** Any compromise that breaks the audit hash chain,
  drops/alters rows without a corresponding audit row, encrypts
  bronze/silver/gold blobs in SeaweedFS, or installs persistent
  backdoors in the application image.
- **Out of scope:** Accidental data loss (dr-1), cross-store drift
  (dr-2), region-level loss (dr-4).

## Detection signals

| Signal | Source | Action |
|---|---|---|
| `audit.verify_hash_chain(NULL, NULL)` returns FALSE | DBA / cron | **DECLARE INCIDENT IMMEDIATELY** |
| Sentry: unauthenticated POSTs to admin endpoints | Sentry alert | Cross-reference with ingress logs |
| New `silver.users` rows the operator didn't provision | Audit cron | Possible escalation backdoor |
| SeaweedFS objects with unexpected MIME types or `.encrypted` suffixes | Backup-agent cron | Ransomware encryption fingerprint |
| Container image hash mismatch vs. signed manifest | image-verify cron | Persistent backdoor candidate |
| Off-cluster forensics: ingress traffic to known-bad IP | Network egress monitor | Active exfiltration |

## RTO / RPO

| Tier | RTO | RPO |
|---|---|---|
| Detection → full lockdown | **5 min** | n/a |
| Forensics window (preserve evidence) | **2-4 hr** | n/a |
| Restore from immutable cold-tier | **8-24 hr** | **≤ 24 hr** daily signed snapshot |
| Resumption (creds rotated, images re-pulled) | **+4 hr** beyond restore | n/a |

The objective is **prove what changed** before restoring. Hot
snapshots are not trustworthy — the adversary may have rewritten
them too. Restoration MUST use the off-cluster signed cold-tier.

---

## Procedure

### Phase A — Lockdown (target: 5 minutes)

**This must happen FAST. Skip nothing.**

1. **Block all writes** — emergency read-only mode:
   ```bash
   docker compose exec -T fastapi python -c "
   import asyncio, os, redis.asyncio as redis
   async def main():
       r = redis.from_url(f'redis://:{os.environ[\"REDIS_PASSWORD\"]}@redis:6379/0')
       await r.set('georag:flags:emergency_lockdown', '1')
   asyncio.run(main())"
   ```
   The orchestrator + workers refuse all writes when set.

2. **Block ingress** at Caddy:
   ```bash
   docker compose exec -T caddy caddy reload --config /etc/caddy/lockdown.json
   ```
   Lockdown config: allow operator IPs only on `/admin/dr`.

3. **Stop ALL worker pools.** No new background activity:
   ```bash
   docker compose stop \
       fastapi laravel-octane laravel-horizon laravel-reverb \
       hatchet hatchet-worker-ai hatchet-worker-ingestion \
       kestra dagster
   ```

4. **DO NOT restart anything yet.** Restart-then-investigate
   destroys forensic state.

### Phase B — Forensics (target: 2-4 hours)

**Goal: identify the entry vector + the timeline of modifications.**
PostgreSQL data is contaminated but its audit ledger is the
single best forensic record we have.

1. **Snapshot the contaminated state.** Take a new snapshot marked
   `contaminated` so it's never confused with a clean one:
   ```bash
   docker compose exec -T postgresql pg_basebackup \
       -U $POSTGRES_USER \
       -D /tmp/contaminated-$(date +%Y%m%d-%H%M%S) \
       -Fp -Pv -X stream
   # Then upload to s3://georag-forensics/contaminated/
   # (separate bucket from standard backups)
   ```

2. **Find the hash-chain break.** Binary-search the audit ledger:
   ```sql
   WITH chain AS (
     SELECT id, action_type, created_at, prev_hash,
            LAG(hash) OVER (ORDER BY id) AS expected_prev_hash
       FROM audit.audit_ledger
   )
   SELECT id, action_type, created_at
     FROM chain
    WHERE prev_hash IS DISTINCT FROM expected_prev_hash
    ORDER BY id LIMIT 5;
   ```
   The earliest row identifies **when** tampering began. Anything
   inserted/modified after that timestamp is suspect.

3. **Catalog the suspect window** — all rows after the break in
   audit + silver + gold + SeaweedFS.

4. **Engage external IR provider** (Kyle's choice; placeholder:
   Mandiant / CrowdStrike). Provide:
   - The contaminated basebackup URI
   - The audit ledger window
   - Container image hashes from the affected window
   - Caddy access logs for the window
   - Sentry events for the window

5. **Network egress isolation.** Cut outbound except IR whitelist.

### Phase C — Recovery preparation (target: 4-8 hours)

While forensics run in parallel:

1. **Identify the last clean snapshot.** Pick the most recent
   immutable cold-tier basebackup **before** the hash-chain break.
   Verify its signature:
   ```bash
   docker compose run --rm fastapi python -c "
   import boto3, os, gnupg
   s3 = boto3.client('s3', endpoint_url=os.environ['SEAWEEDFS_ENDPOINT_URL'])
   obj = s3.get_object(Bucket='georag-backups-immutable',
                       Key='postgres/basebackup-CLEAN.tar.gz.asc')
   gpg = gnupg.GPG()
   v = gpg.verify(obj['Body'].read())
   assert v.valid, f'Signature invalid: {v.problems}'
   print(f'OK — signed by {v.username}')"
   ```

2. **Rotate ALL credentials.** Every secret in the running `.env` is
   suspect. POSTGRES_PASSWORD, NEO4J_PASSWORD, REDIS_PASSWORD,
   SEAWEEDFS keys, JWT signing key, Sanctum keys,
   FASTAPI_SERVICE_KEY, ANTHROPIC_API_KEY, KESTRA_FLOW_AUTH_TOKEN,
   PAGERDUTY_INTEGRATION_KEY. Use the
   `docs/RUNBOOK.md` → secret-rotation procedure.

3. **Pull fresh container images** from registered SHA digests:
   ```bash
   docker compose pull --include-deps --quiet
   docker image prune -a -f  # purge cached local layers
   ```

### Phase D — Restoration (target: 8-24 hours)

1. **Provision clean target hosts.** New VMs/containers; the
   contaminated ones may have persistent backdoors. Do NOT re-use
   Docker volumes.

2. **Restore Postgres from the signed clean snapshot.** Same
   mechanics as dr-1 Phase B but **NO WAL replay past the
   hash-chain break point**.

3. **Rebuild downstream stores via dr-2 procedure** (`restore_workspace`
   workflow). Run for EVERY workspace — the adversary may have
   planted backdoors in unrelated tenants.

4. **Verify the new audit chain** is intact:
   ```sql
   SELECT audit.verify_hash_chain(NULL, NULL);
   ```

5. **Re-engage Caddy normal config; drop emergency_lockdown flag.**

### Phase E — Post-restoration hardening (ongoing)

1. IR provider's forensic report → `ops/incidents/<ticket-id>/`
   (private, encrypted).
2. Disclose to customers per §29.4 breach-disclosure SLA.
3. Update WAL retention + immutable snapshot frequency if the
   timeline showed gaps.
4. Network segmentation review — were ingress + egress rules
   tight enough to catch the entry vector?
5. Full SBOM audit against the new image set; bump any transitive
   dep flagged by the IR finding.

## Post-mortem

DR-3 incidents always produce a customer-facing post-mortem. Draft
template: `docs/post-mortem-template.md`. Required sections:
- Timeline (with timezones)
- Detection signal
- Impact (workspaces affected, data classes, hours blocked)
- Root cause (entry vector + propagation path)
- Fix (this runbook's execution)
- Hardening actions taken
- Lessons learned via `record_decision()`

## Open questions for Kyle

1. **Immutable snapshot cadence.** Today daily; tighter (every 4h)
   shrinks RPO bound but costs more SeaweedFS space. Default
   proposal: daily + weekly with 90-day retention.
2. **IR provider selection.** Final pick is Kyle's contract + SLA.
3. **Customer disclosure timing.** §29.4 placeholder "within 72
   hours" — GDPR-aligned. US-state laws vary (Maine: 30 days;
   California: 45 days; New York: ASAP). May need per-jurisdiction
   split.
