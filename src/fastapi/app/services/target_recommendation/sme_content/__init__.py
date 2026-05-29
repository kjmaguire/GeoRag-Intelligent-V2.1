"""SME-authored deposit model content (§8.3, doc-phase 123).

This package holds the Kyle-edited deposit-model attributes that
populate `targeting.target_models` + `targeting.target_model_versions`.
Per master-plan §8.3 + §20.2, this content is SME territory — auto-
populating from public sources would compromise the R5 sign-off
story for any target recommendation that flows out of it.

Pattern:
    1. Edit a content module here (e.g. `athabasca_uranium.py`).
    2. Run the seeder:
           docker exec georag-fastapi python -m \
               app.services.target_recommendation.sme_content \
               --slug athabasca_uranium --activate
    3. Verify via pytest:
           docker exec georag-fastapi pytest \
               tests/test_sme_seeders.py -q

The seeder is idempotent — re-running with the same slug updates
the existing target_models row + creates a new target_model_version
each time (so version history is preserved for §18.3 A/B comparison
+ §29.6 regulatory traceability).
"""
from app.services.target_recommendation.sme_content.seed_runner import (
    SmeSeedResult,
    seed_deposit_model_from_module,
)

__all__ = [
    "SmeSeedResult",
    "seed_deposit_model_from_module",
]
