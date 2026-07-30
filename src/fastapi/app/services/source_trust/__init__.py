"""Source trust retrieval-ranking extension (§12.8) — doc-phase 102.

Provides the source-trust boost used by retrieval-ranking callers.

Live behavior lands when:
- §12.7 train_source_trust workflow ships labeled trust scores
- retrieval ranking reads source_trust_scores per chunk

This module is the SINGLE function ranking-layer callers invoke
(boost_by_trust).
"""
from app.services.source_trust.boost import boost_by_trust

__all__ = ["boost_by_trust"]
