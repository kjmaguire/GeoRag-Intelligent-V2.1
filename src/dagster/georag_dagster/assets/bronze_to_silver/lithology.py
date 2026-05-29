"""bronze.raw_lithology_logs → silver.lithology.

Transforms:
  1. Resolve hole_id (text) → collar_id (UUID) via silver.collars
  2. Look up rock_name → standardised rock_code via silver.rock_codes
     a. Exact lowercase match in preferred system → confidence 1.0
     b. Exact lowercase match in fallback system → confidence 1.0
     c. rapidfuzz fuzzy match across both systems with similarity
        >= preferred_match_threshold → confidence = (similarity / 100)
     d. No usable match → rock_code NULL, confidence NULL (catalogue gap)
  3. UPSERT into silver.lithology keyed on bronze_source_id
"""
import psycopg2.extras
from dagster import (
    AssetExecutionContext,
    Config,
    MaterializeResult,
    MetadataValue,
    asset,
)
from rapidfuzz import fuzz, process

from georag_dagster.resources import PostgresResource


class SilverLithologyConfig(Config):
    workspace_id: str
    preferred_system: str = "NRCAN"  # 'NRCAN' or 'GSC'
    # Minimum rapidfuzz token-set ratio (0-100) for a fuzzy match to be
    # accepted. 60 catches "granitic" → "Granite" and "qtz monz" →
    # "Quartz Monzonite" without bleeding into unrelated rocks. Tune up
    # if false positives appear in the review queue.
    preferred_match_threshold: int = 60


_UPSERT_SQL = """
INSERT INTO silver.lithology (
    workspace_id, collar_id, from_depth, to_depth,
    rock_code, rock_code_confidence, rock_name,
    description, colour, grain_size,
    logged_by, logged_date, bronze_source_id
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (id) DO NOTHING
"""


def resolve_rock_code(
    rock_name: str | None,
    code_map: dict[str, dict[str, str]],
    preferred_system: str,
    fuzzy_threshold: int = 60,
) -> tuple[str | None, float | None]:
    """Map a free-text rock name to a (code, confidence) tuple.

    Exact lowercase match in the preferred system wins with confidence 1.0;
    same in the fallback system also wins with 1.0. If neither has an
    exact hit, rapidfuzz token-set ratio is computed across the union of
    both systems' names. The best score above ``fuzzy_threshold`` is
    accepted with confidence = score / 100.

    Returns (None, None) when nothing matches above the threshold; the
    caller writes the row with rock_code NULL so the catalogue gap stays
    visible to operators.
    """
    if not rock_name:
        return None, None

    needle = rock_name.strip().lower()
    if not needle:
        return None, None

    pref = code_map.get(preferred_system, {})
    if needle in pref:
        return pref[needle], 1.0

    other = "GSC" if preferred_system == "NRCAN" else "NRCAN"
    other_map = code_map.get(other, {})
    if needle in other_map:
        return other_map[needle], 1.0

    # Union both systems for the fuzzy pass. Preferred entries take
    # precedence on a tie by being inserted first.
    candidates: dict[str, str] = {}
    candidates.update(pref)
    for k, v in other_map.items():
        candidates.setdefault(k, v)

    if not candidates:
        return None, None

    best = process.extractOne(
        needle,
        candidates.keys(),
        scorer=fuzz.token_set_ratio,
        score_cutoff=fuzzy_threshold,
    )
    if best is None:
        return None, None

    matched_name, score, _idx = best
    # Cap fuzzy results below 1.0 so 1.0 is reserved for the
    # exact-equality codepath above. Without this, token_set_ratio
    # returns 100 for "weathered granite" ⊃ "granite" — semantically
    # right but indistinguishable from a true exact match, which kills
    # the review signal the confidence column exists to provide.
    confidence = min(float(score) / 100.0, 0.99)
    return candidates[matched_name], confidence


# Back-compat shim: the original private helper name. Returns code only.
def _resolve_rock_code(
    rock_name: str | None,
    code_map: dict[str, dict[str, str]],
    preferred_system: str,
) -> str | None:
    code, _ = resolve_rock_code(rock_name, code_map, preferred_system)
    return code


@asset(
    group_name="drillhole_silver",
    description=(
        "Validated lithology intervals. Resolves rock_name → rock_code "
        "via silver.rock_codes (NRCAN preferred). Exact match wins with "
        "confidence 1.0; fuzzy match via rapidfuzz accepts >= "
        "preferred_match_threshold and writes the score to "
        "silver.lithology.rock_code_confidence. Reads "
        "bronze.raw_lithology_logs, writes silver.lithology."
    ),
)
def silver_lithology_v2(
    context: AssetExecutionContext,
    config: SilverLithologyConfig,
    postgres: PostgresResource,
) -> MaterializeResult:
    pg = postgres.connect()
    rows_in = 0
    rows_written = 0
    rows_skipped_unknown_hole = 0
    rows_unmapped_rock_name = 0
    rows_fuzzy_matched = 0

    try:
        with pg.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Build the hole→collar map.
            cur.execute(
                "SELECT hole_id, collar_id FROM silver.collars WHERE workspace_id = %s",
                (config.workspace_id,),
            )
            hole_to_collar = {r["hole_id"]: r["collar_id"] for r in cur.fetchall()}

            # Build rock_code map keyed by system → lower(name) → code.
            cur.execute(
                "SELECT system, code, name FROM silver.rock_codes WHERE workspace_id = %s::uuid",
                (config.workspace_id,),
            )
            code_map: dict[str, dict[str, str]] = {}
            for r in cur.fetchall():
                code_map.setdefault(r["system"], {})[r["name"].lower()] = r["code"]

            cur.execute(
                """
                SELECT id, hole_id, from_depth, to_depth, rock_name,
                       description, colour, grain_size, logged_by, logged_date
                  FROM bronze.raw_lithology_logs
                 WHERE workspace_id = %s::uuid
                """,
                (config.workspace_id,),
            )
            bronze_rows = cur.fetchall()

        rows_in = len(bronze_rows)
        with pg.cursor() as upsert_cur:
            for row in bronze_rows:
                collar_id = hole_to_collar.get(row["hole_id"])
                if collar_id is None:
                    rows_skipped_unknown_hole += 1
                    continue

                if row["to_depth"] is None or row["from_depth"] is None \
                   or row["to_depth"] <= row["from_depth"]:
                    continue

                rock_code, confidence = resolve_rock_code(
                    row["rock_name"], code_map, config.preferred_system,
                    fuzzy_threshold=config.preferred_match_threshold,
                )
                if rock_code is None and row["rock_name"]:
                    rows_unmapped_rock_name += 1
                    # We still write the row — rock_code stays NULL so
                    # the catalogue gap is visible in queries.
                elif confidence is not None and confidence < 1.0:
                    rows_fuzzy_matched += 1

                upsert_cur.execute(
                    _UPSERT_SQL,
                    (
                        config.workspace_id, collar_id,
                        row["from_depth"], row["to_depth"],
                        rock_code, confidence, row["rock_name"],
                        row["description"], row["colour"], row["grain_size"],
                        row["logged_by"], row["logged_date"],
                        row["id"],
                    ),
                )
                rows_written += 1

        pg.commit()
    finally:
        pg.close()

    return MaterializeResult(
        metadata={
            "rows_in": MetadataValue.int(rows_in),
            "rows_written": MetadataValue.int(rows_written),
            "rows_skipped_unknown_hole": MetadataValue.int(rows_skipped_unknown_hole),
            "rows_unmapped_rock_name": MetadataValue.int(rows_unmapped_rock_name),
            "rows_fuzzy_matched": MetadataValue.int(rows_fuzzy_matched),
            "fuzzy_threshold": MetadataValue.int(config.preferred_match_threshold),
        },
    )
