# Data Dictionary — Index

Status: **Skeleton.** Per-schema files below are placeholders awaiting
the `data_dictionary_dump` Dagster asset described in
[Appendix F §1](../appendix/F-data-dictionary.md#1-generator-design).
Until the generator lands, [Ch 03 — Schemas and Tables](../manual/03-schemas.md)
is the human-curated reference.

## Core tables (full column data, hand-curated)

[**_core_tables.md**](_core_tables.md) — five highest-traffic tables
with verified column lists, constraints, indexes, triggers, RLS, and
read/write attribution:
`silver.collars`, `silver.answer_runs`, `audit.audit_ledger`,
`bronze.provenance`, `gold.significant_intersections`.

## Schemas

| Schema | Generator output | Status |
|---|---|---|
| `bronze` | [bronze.md](bronze.md) | Skeleton |
| `silver` | [silver.md](silver.md) | Skeleton |
| `gold` | [gold.md](gold.md) | Skeleton |
| `audit` | [audit.md](audit.md) | Skeleton |
| `usage` | [usage.md](usage.md) | Skeleton |
| `outbox` | [outbox.md](outbox.md) | Skeleton |
| `workflow` | [workflow.md](workflow.md) | Skeleton |
| `workspace` | [workspace.md](workspace.md) | Skeleton |
| `public_geo` | [public_geo.md](public_geo.md) | Skeleton |
| `interpretation` | [interpretation.md](interpretation.md) | Skeleton |
| `targeting` | [targeting.md](targeting.md) | Skeleton |
| `ops` | [ops.md](ops.md) | Skeleton |
| `eval` | [eval.md](eval.md) | Skeleton |
| `public` | [public.md](public.md) | Skeleton |

## Why skeleton

The full per-table dump would be ~250 tables × ~30 lines = ~7,500 lines
of mostly-auto-generatable content. Hand-curating it now would diverge
from the live DB inside a week. The right path is
[Appendix F §1's generator](../appendix/F-data-dictionary.md#1-generator-design)
plus a CI drift guard.

Each schema file below carries the minimum a hand-reader needs *right
now*: pointer to Ch 03, list of tables in the schema, and the migration
that created each. That gives a navigable starting point until the
generator runs.

## ERD output

Will land under [../erd/](../erd/) once the generator + SchemaSpy
(or `eralchemy2`) run wires up.
