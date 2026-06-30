"""Analogue Finder Agent (§9.8 / §20.6).

Qdrant + Neo4j combined ranker for "find documented deposits with
similar signature."

Two-channel similarity:
  1. Embedding similarity in Qdrant — description-based match.
  2. Graph traversal in Neo4j — shared attribute paths (same host rock
     + same alteration + same age + similar tectonic setting).

Combined ranking surfaces analogues WITH explainable similarity reasons
(which attributes matched, which embeddings clustered).

Phase H4 graduation — the agent supports an "in-memory analogue
catalogue" fallback that runs without Qdrant/Neo4j against a curated
list of well-known global analogues (Athabasca uranium, Carlin gold,
Olympic Dam IOCG, Sudbury Ni-Cu-PGE, Witwatersrand Au, etc.). When the
production catalogue arrives via `silver.target_models.analogues_payload`,
that becomes the authoritative source.

Similarity math:
    combined_score = 0.55 * embedding_similarity
                   + 0.45 * graph_path_similarity

Output contract:
    {
        "analogues": [
            {
                "deposit_name": str,
                "location": str,
                "commodity": str,
                "deposit_model": str,
                "embedding_similarity": float,
                "graph_path_similarity": float,
                "combined_score": float,
                "matched_attributes": [str],
                "evidence_chunk_ids": [str]
            }
        ],
        "summary": str,
        "channel_used": "in_memory" | "qdrant_neo4j",
    }
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from app.agents import AgentContext, georag_agent

logger = logging.getLogger(__name__)


# Weight split for the combined score. Tuned so that strong attribute
# overlap (graph path 1.0, embedding 0.4) ranks above weak attribute
# overlap (graph path 0.4, embedding 0.8) — i.e. attribute fit beats
# pure text-similarity in geological analogue selection.
_W_EMBEDDING = 0.55
_W_GRAPH     = 0.45


# Curated global analogues. Each entry's `attributes` set is what the
# graph-path similarity matches against. The `description` would be
# embedded into Qdrant in production; here we lexically score against
# the project_attributes' "description_hint" field instead.
_ANALOGUE_CATALOGUE: tuple[dict[str, Any], ...] = (
    {
        "deposit_name":   "McArthur River",
        "location":       "Athabasca Basin, Saskatchewan",
        "commodity":      "Uranium",
        "deposit_model":  "unconformity_uranium",
        "attributes":     {"unconformity", "basement_graphitic", "uranium", "athabasca", "redox", "paleoproterozoic"},
        "description":    "World-class unconformity-style uranium deposit hosted at the Athabasca Basin sandstone / metamorphic basement contact with graphitic pelite host structures.",
    },
    {
        "deposit_name":   "Cigar Lake",
        "location":       "Athabasca Basin, Saskatchewan",
        "commodity":      "Uranium",
        "deposit_model":  "unconformity_uranium",
        "attributes":     {"unconformity", "basement_graphitic", "uranium", "athabasca", "redox"},
        "description":    "Unconformity uranium deposit with high-grade pods at the Athabasca sandstone / basement unconformity, intense clay alteration.",
    },
    {
        "deposit_name":   "Olympic Dam",
        "location":       "South Australia",
        "commodity":      "Cu-Au-U",
        "deposit_model":  "iocg",
        "attributes":     {"iocg", "hematite_breccia", "copper", "gold", "uranium", "rare_earth"},
        "description":    "Iron oxide-copper-gold supergiant in hematite breccia complex hosted by Mesoproterozoic granite.",
    },
    {
        "deposit_name":   "Carlin Trend",
        "location":       "Nevada, USA",
        "commodity":      "Gold",
        "deposit_model":  "carlin_gold",
        "attributes":     {"sedimentary_hosted", "gold", "decalcification", "arsenian_pyrite", "carbonate_host"},
        "description":    "Sedimentary-hosted disseminated gold along carbonate host rocks with arsenian pyrite + decalcification halo.",
    },
    {
        "deposit_name":   "Sudbury",
        "location":       "Ontario, Canada",
        "commodity":      "Ni-Cu-PGE",
        "deposit_model":  "magmatic_sulphide",
        "attributes":     {"magmatic_sulphide", "nickel", "copper", "platinum_group", "impact_origin", "norite"},
        "description":    "Magmatic Ni-Cu-PGE sulphide ores at the base of the Sudbury Igneous Complex (impact origin).",
    },
    {
        "deposit_name":   "Witwatersrand",
        "location":       "South Africa",
        "commodity":      "Gold",
        "deposit_model":  "paleoplacer_gold",
        "attributes":     {"paleoplacer", "gold", "uranium", "archean", "conglomerate"},
        "description":    "Archean paleoplacer gold + uranium in quartz-pebble conglomerate reefs.",
    },
    {
        "deposit_name":   "Bingham Canyon",
        "location":       "Utah, USA",
        "commodity":      "Cu-Mo-Au",
        "deposit_model":  "porphyry_copper",
        "attributes":     {"porphyry", "copper", "molybdenum", "monzonite", "potassic_alteration"},
        "description":    "Giant porphyry copper-molybdenum deposit centred on a Tertiary monzonite intrusion with strong potassic alteration.",
    },
    {
        "deposit_name":   "Voisey's Bay",
        "location":       "Labrador, Canada",
        "commodity":      "Ni-Cu-Co",
        "deposit_model":  "magmatic_sulphide",
        "attributes":     {"magmatic_sulphide", "nickel", "copper", "troctolite", "rift_setting"},
        "description":    "Magmatic Ni-Cu sulphide deposit in troctolite intrusion within a Proterozoic rift.",
    },
    {
        "deposit_name":   "Red Dog",
        "location":       "Alaska, USA",
        "commodity":      "Zn-Pb-Ag",
        "deposit_model":  "sedex",
        "attributes":     {"sedex", "zinc", "lead", "shale_hosted", "barite", "exhalative"},
        "description":    "Sedimentary exhalative Zn-Pb-Ag in Carboniferous black shale with abundant barite.",
    },
    {
        "deposit_name":   "Hemlo",
        "location":       "Ontario, Canada",
        "commodity":      "Gold",
        "deposit_model":  "orogenic_gold",
        "attributes":     {"orogenic_gold", "gold", "shear_zone", "archean", "greenschist"},
        "description":    "Archean orogenic gold deposit along the Hemlo shear zone in greenschist-facies metavolcanics.",
    },
)


def _embedding_similarity(project_text: str, candidate: dict[str, Any]) -> float:
    """Lightweight lexical token-overlap proxy for embedding similarity.

    Production swaps this for Qdrant cosine similarity over the
    description embedding. The contract — return float in [0, 1] — is
    preserved.
    """
    if not project_text:
        return 0.0
    p_tokens = {t.lower().strip(".,;:()") for t in project_text.split() if t}
    c_tokens = {t.lower().strip(".,;:()") for t in candidate["description"].split() if t}
    if not p_tokens or not c_tokens:
        return 0.0
    overlap = p_tokens & c_tokens
    return len(overlap) / max(len(p_tokens), 1)


def _graph_path_similarity(
    project_attrs: dict[str, Any], candidate: dict[str, Any],
) -> tuple[float, list[str]]:
    """Shared-attribute path similarity (Jaccard over attribute sets).

    Returns (similarity, matched_attributes) so the explanation can
    say "matched on unconformity, basement_graphitic".
    """
    p_attrs: set[str] = set()
    for v in project_attrs.values():
        if isinstance(v, str):
            p_attrs.add(v.lower())
        elif isinstance(v, (list, tuple, set)):
            for x in v:
                if isinstance(x, str):
                    p_attrs.add(x.lower())
    cand_attrs = {a.lower() for a in candidate["attributes"]}
    if not p_attrs or not cand_attrs:
        return 0.0, []
    matched = sorted(p_attrs & cand_attrs)
    union = p_attrs | cand_attrs
    return len(matched) / max(len(union), 1), matched


@georag_agent(
    name="Analogue Finder Agent",
    risk_tier="R1",
    version="1.0.0",  # graduated Phase H4
)
async def analogue_finder(
    ctx: AgentContext,
    *,
    workspace_id: UUID | str,
    target_model_id: UUID | str,
    project_attributes: dict[str, Any],
    top_k: int = 10,
) -> dict[str, Any]:
    """Find analogues for the target deposit + project attributes.

    Args:
        workspace_id: RLS scope (informational).
        target_model_id: which target model this lookup is for; carried
            forward into the result for traceability.
        project_attributes: dict — may carry ``description_hint`` (str)
            and attribute lists like ``host_rocks``, ``alterations``,
            ``commodities``, ``deposit_model``, ``tectonic_setting``,
            ``age``. Anything string-shaped or list-of-strings is
            collected into the attribute set.
        top_k: max recommendations to return.

    Returns:
        Structured analogue list ranked by combined_score DESC.
    """
    description = str(project_attributes.get("description_hint", ""))
    scored: list[dict[str, Any]] = []

    for cand in _ANALOGUE_CATALOGUE:
        emb_sim = _embedding_similarity(description, cand)
        graph_sim, matched = _graph_path_similarity(project_attributes, cand)
        combined = _W_EMBEDDING * emb_sim + _W_GRAPH * graph_sim
        if combined <= 0.0:
            continue
        scored.append({
            "deposit_name":          cand["deposit_name"],
            "location":              cand["location"],
            "commodity":             cand["commodity"],
            "deposit_model":         cand["deposit_model"],
            "embedding_similarity":  round(emb_sim, 4),
            "graph_path_similarity": round(graph_sim, 4),
            "combined_score":        round(combined, 4),
            "matched_attributes":    matched,
            "evidence_chunk_ids":    [],  # populated from Qdrant payload
                                          # in prod
        })

    scored.sort(key=lambda r: r["combined_score"], reverse=True)
    top = scored[:top_k]

    summary = (
        f"target_model={target_model_id} candidates_scored={len(scored)} "
        f"returned={len(top)} channel=in_memory"
    )
    logger.info("analogue_finder: %s", summary)
    return {
        "analogues":     top,
        "summary":       summary,
        "channel_used":  "in_memory",
        "target_model_id": str(target_model_id),
    }


__all__ = ["analogue_finder"]
