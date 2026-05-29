"""Source trust retrieval-ranking extension (§12.8) — doc-phase 102.

Feeds `silver.source_trust_scores` into the existing retrieval
fusion layer (`app.services.fusion`). The fusion function combines
embedding similarity + lexical similarity + source trust into a
single retrieval score.

Live behavior lands when:
- §12.7 train_source_trust workflow ships labeled trust scores
- the fusion layer extension reads source_trust_scores per chunk

This module is the SINGLE function ranking-layer callers invoke
(boost_by_trust). Schema-compatible with the existing fusion API.
"""
from app.services.source_trust.boost import boost_by_trust

__all__ = ["boost_by_trust"]
