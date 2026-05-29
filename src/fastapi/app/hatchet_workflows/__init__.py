"""Phase 0 step 4 — Hatchet workflows for audit verification + outbox dispatch.

Two workflows live here:

* ``audit_ledger_verify`` — nightly cron (02:00 UTC) that calls the
  pure-SQL ``audit.run_verification`` function for the previous 24 h
  window. Writes one row to ``audit.audit_ledger_verification_runs``
  with status ``clean`` / ``break`` / ``error``.
* ``outbox_dispatcher`` — every-minute cron that drains
  ``outbox.pending_propagations`` to Qdrant / Neo4j / SeaweedFS, records
  every attempt to ``outbox.propagation_attempts``, and dead-letters
  rows after ``dead_letter_after_attempts`` transient failures.

The shared ``Hatchet`` client lives at module scope so the worker
entrypoint and both workflow modules import the same instance — that's
what the SDK's worker registration relies on.

Auth/connection env vars (all read by the SDK directly):

* ``HATCHET_CLIENT_TOKEN`` — JWT minted via
  ``hatchet-admin token create --tenant-id <default-tenant-id>`` inside
  the ``georag-hatchet`` container. Required.
* ``HATCHET_CLIENT_HOST_PORT`` — overrides the broadcast address baked
  into the token. Set to ``hatchet-lite:7077`` for in-cluster workers.
* ``HATCHET_CLIENT_TLS_STRATEGY`` — ``none`` for the Hatchet Lite dev
  deployment (gRPC is plaintext per ``SERVER_GRPC_INSECURE=t``).
"""

from __future__ import annotations

from hatchet_sdk import Hatchet

hatchet = Hatchet()

__all__ = ["hatchet"]
