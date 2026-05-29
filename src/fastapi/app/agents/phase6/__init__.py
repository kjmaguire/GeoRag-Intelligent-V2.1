"""Master-plan §6 visual + retrieval agents (doc-phase 75 skeletons).

Currently:
- Public/Private Boundary Agent (§6.4) — enforces §2.9 language posture
  at every retrieval; tags chunks with data_visibility = public | workspace
"""
from app.agents.phase6.public_private_boundary import public_private_boundary

__all__ = ["public_private_boundary"]
