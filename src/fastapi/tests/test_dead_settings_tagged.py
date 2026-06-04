"""Audit item H — pin the DEAD SETTING tags in config.py.

Background — the earlier audit (AUDIT_AND_FIX_REPORT.md) claimed 22
"dead" settings remaining after the 4 LLM_FALLBACK_* tags landed. Item H
re-verified each candidate and found that the audit had vastly
overcounted: most candidates were read via ``getattr(settings, "NAME",
default)`` rather than ``settings.NAME`` direct access, and the original
sweep only grepped for the direct form.

Genuinely-dead set after re-verification: TWO settings.

  1. ``SUMMARIZER_ENABLED``  — gate flag that no code consults; the
     corpus_summarizer module runs unconditionally when called via
     ``summarize_scope(...)``, and the orchestrator never calls it.
  2. ``TEMPERATURE_BY_QUERY_TYPE`` — ClassVar dict added per
     OVERNIGHT_LOG step 2b but never imported. VLLM_TEMPERATURE is the
     single global temperature actually applied.

What these tests pin
--------------------
The DEAD SETTING warning block stays present on each field so the next
contributor sees the "wire or delete" branch BEFORE writing a third
reader. If a contributor genuinely wires the setting, the test gets
deleted in the same PR (the test name + assertion explain why).

Why pin via file content and not the field
------------------------------------------
The fields ARE legal Pydantic state — Settings has to import cleanly —
so we can't assert "settings.SUMMARIZER_ENABLED doesn't exist". We pin
the warning comment instead so a future "remove the comment" cleanup
trips this test and forces the contributor to either remove the setting
or remove the test.
"""
from __future__ import annotations

from pathlib import Path


CONFIG_PY = Path(__file__).resolve().parents[1] / "app" / "config.py"


def _config_text() -> str:
    return CONFIG_PY.read_text(encoding="utf-8")


def test_summarizer_enabled_carries_dead_setting_tag() -> None:
    src = _config_text()

    # The phrase 'DEAD SETTING (audit item H' must appear near the
    # SUMMARIZER_ENABLED declaration; the easiest pin is "both literals
    # exist + they are within 800 chars of each other" (the whole block
    # is ~1KB). Cross-check via the next-action language so a partial
    # rename (removing 'audit item H' but leaving 'DEAD SETTING') still
    # passes — the rationale stays load-bearing.
    flag_idx = src.find("SUMMARIZER_ENABLED: bool = False")
    assert flag_idx != -1, (
        "SUMMARIZER_ENABLED field declaration is missing. If the field "
        "was actually removed, delete this test too."
    )

    # Look backwards from the field for the warning comment block.
    window = src[max(0, flag_idx - 1500): flag_idx]
    assert "DEAD SETTING (audit item H" in window, (
        "SUMMARIZER_ENABLED must carry the 'DEAD SETTING (audit item H, "
        "2026-06-03) — NO READERS' warning block immediately above the "
        "declaration. Item H pinned this so the next contributor doesn't "
        "add a THIRD half-wired reader. If you intentionally wired the "
        "flag — delete this test, not the comment."
    )
    assert "Wire it: add IntentRoute.SUMMARIZE" in window, (
        "The DEAD SETTING block on SUMMARIZER_ENABLED must include the "
        "WIRE-OR-DELETE choice. Future-you might forget which decision is "
        "outstanding; the comment carries the next action."
    )


def test_temperature_by_query_type_carries_dead_setting_tag() -> None:
    src = _config_text()

    flag_idx = src.find("TEMPERATURE_BY_QUERY_TYPE: ClassVar")
    assert flag_idx != -1, (
        "TEMPERATURE_BY_QUERY_TYPE field declaration is missing. If the "
        "field was actually removed, delete this test too."
    )

    window = src[max(0, flag_idx - 1500): flag_idx]
    assert "DEAD SETTING (audit item H" in window, (
        "TEMPERATURE_BY_QUERY_TYPE must carry the audit item H DEAD "
        "SETTING warning. The dict has been sitting unused since "
        "OVERNIGHT_LOG step 2b; without the tag a future contributor "
        "will look at the rich per-intent values and assume the wiring "
        "exists."
    )
    assert "VLLM_TEMPERATURE" in window, (
        "The DEAD SETTING block must point readers at VLLM_TEMPERATURE "
        "as the actually-applied single global temperature, so the "
        "wire-vs-delete decision can be made with full context."
    )


def test_dead_setting_inventory_matches_documented_count() -> None:
    """Pin the count so future drift (silent additions / removals) shows up.

    Two settings are tagged today. If the count drops (a contributor
    wired one), this test fails and forces them to also delete this
    test entry — which is the whole point of the pin: changes to the
    dead-setting inventory should be explicit, not silent.
    """
    src = _config_text()
    # Each tagged block is anchored by "DEAD SETTING (audit item H".
    # Count occurrences directly.
    count = src.count("DEAD SETTING (audit item H")
    assert count == 2, (
        f"Expected exactly 2 DEAD SETTING (audit item H) tags in "
        f"config.py, found {count}. If you ADDED a new dead-setting tag, "
        f"bump this assertion + extend test coverage with a per-field "
        f"helper. If you REMOVED one (wired or deleted the setting), drop "
        f"the assertion entry — silent count drift defeats the pin."
    )
