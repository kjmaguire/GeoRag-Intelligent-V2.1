#!/usr/bin/env bash
# =============================================================================
# scripts/phase11_step1_verify.sh
#
# Phase 11 Step 1 done-definition — Section 04i hallucination layer
# audit doc.
#
#   1. docs/phase11_section_04i_audit.md exists + non-trivial
#   2. Doc covers all six §04i layers (Layer 1..6)
#   3. Doc references the four §04i v1.49 guards (Numeric grounding,
#      Entity grounding, Citation completeness, Refusal path)
#   4. Doc lists ≥5 gap observations
#   5. Each layer file referenced by the doc actually exists at the
#      stated path
#   6. Implementation summary table has at least 9 rows (six layers
#      + completeness + orchestrator validators + qualitative)
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=6
REPO="${REPO:-/home/georag/projects/georag}"
DOC="$REPO/docs/phase11_section_04i_audit.md"
HALLU_DIR="$REPO/src/fastapi/app/agent/hallucination"

check() {
    if [ "$2" = ok ]; then
        echo "  [PASS] $1"
        PASS=$((PASS+1))
    else
        echo "  [FAIL] $1 — $3"
    fi
}

cat <<'BANNER'

============================================================
PHASE 11 STEP 1 — SECTION 04i AUDIT DOC VERIFICATION
============================================================
BANNER

# 1) Doc exists + non-trivial
if [ -s "$DOC" ]; then
    lines=$(wc -l < "$DOC")
    if [ "$lines" -ge 150 ]; then
        check "Audit doc present ($lines lines)" ok
    else
        check "doc length" fail "only $lines lines — needs at least 150"
    fi
else
    check "doc exists" fail "missing"
fi

# 2) Six layers covered
missing_layers=()
for layer in "Layer 1" "Layer 2" "Layer 3" "Layer 4" "Layer 5" "Layer 6"; do
    grep -q "### $layer" "$DOC" || missing_layers+=("$layer")
done
if [ "${#missing_layers[@]}" -eq 0 ]; then
    check "All six §04i layers covered as separate sections" ok
else
    check "layer coverage" fail "missing: ${missing_layers[*]}"
fi

# 3) Four guards referenced
guards_found=0
for guard in "Numeric grounding" "Entity grounding" "Citation completeness" "Refusal path"; do
    if grep -q "$guard" "$DOC"; then
        guards_found=$((guards_found + 1))
    fi
done
[ "$guards_found" = "4" ] \
    && check "All four §04i v1.49 guards referenced" ok \
    || check "guard mapping" fail "only $guards_found / 4 guard names present"

# 4) Gap observations
gap_bullets=$(awk '
    /^## / && in_gaps { in_gaps=0 }
    in_gaps && /^[0-9]+\. / { count++ }
    /^## .*[Gg]ap/ { in_gaps=1 }
    END { print count + 0 }
' "$DOC")
[ "${gap_bullets:-0}" -ge 5 ] 2>/dev/null \
    && check "Gaps section lists $gap_bullets observations" ok \
    || check "gaps count" fail "only $gap_bullets / 5+"

# 5) Layer files referenced in doc exist on disk
missing_files=()
for layer_file in layer1_retrieval.py layer2_typed_output.py layer3_numerical.py \
                  layer4_entity.py layer5_provenance.py layer6_constraints.py \
                  layer_completeness.py orchestrator_validators.py qualitative_detector.py; do
    [ -f "$HALLU_DIR/$layer_file" ] || missing_files+=("$layer_file")
    grep -q "$layer_file" "$DOC" || missing_files+=("(not-in-doc:$layer_file)")
done
if [ "${#missing_files[@]}" -eq 0 ]; then
    check "All 9 layer files exist + are referenced in the doc" ok
else
    check "file references" fail "${missing_files[*]}"
fi

# 6) Implementation summary table — count rows starting with `|` and
# containing a layer file name reference
table_rows=$(grep -cE '^\| (1|2|3|4|5|6|completeness|orchestrator validators|qualitative) \|' "$DOC")
[ "${table_rows:-0}" -ge 9 ] 2>/dev/null \
    && check "Implementation summary table has $table_rows rows" ok \
    || check "summary table" fail "got $table_rows / 9+"

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
