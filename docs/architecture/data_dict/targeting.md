# Schema `targeting` — Data Dictionary (skeleton)

Created by [2026_05_13_100000](../../../database/migrations/2026_05_13_100000_create_targeting_schema.php).

## Tables

| Table | Purpose | Status |
|---|---|---|
| `targeting.target_backtests` | Per-backtest run output (recall/precision on labelled targets) | Live |
| `targeting.target_score_factors` | Per-target factor contributions (e.g., proximity, grade, structure) | Live |
| `targeting.target_uncertainties` | Per-target uncertainty breakdown for the Targets page | Live |

## Writers

- Hatchet `score_targets` workflow (manual + scheduled trigger).
- Hatchet `train_target_model` (experimental — refreshes model artefacts).

## Reader

Frontend [Targets.tsx](../../../resources/js/Pages/Foundry/Targets.tsx) +
[TargetRecommendation.tsx](../../../resources/js/Pages/Dashboards/TargetRecommendation.tsx).
