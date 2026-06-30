"""Unit tests for plan §3e multi-turn context resolution."""

from __future__ import annotations

from app.agent.multi_turn_resolver import (
    ConversationTurn,
    EntityMention,
    ResolvedQuery,
    extract_entity_mentions,
    resolve_multi_turn,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _turn(
    idx: int,
    text: str,
    *,
    role: str = "user",
    mentions: list[EntityMention] = None,
) -> ConversationTurn:
    return ConversationTurn(
        turn_index=idx,
        role=role,
        text=text,
        entity_mentions=tuple(mentions or []),
    )


def _hole(idx: int, surface: str) -> EntityMention:
    return EntityMention(
        surface_form=surface, entity_type="hole", turn_index=idx,
    )


def _property_mention(idx: int, surface: str) -> EntityMention:
    return EntityMention(
        surface_form=surface, entity_type="property", turn_index=idx,
    )


# ---------------------------------------------------------------------------
# Empty / no-op paths
# ---------------------------------------------------------------------------


def test_empty_query_returns_unchanged():
    result = resolve_multi_turn("", [])
    assert isinstance(result, ResolvedQuery)
    assert result.rewritten_query == ""
    assert result.made_changes is False
    assert result.overall_confidence == 1.0


def test_empty_history_returns_query_unchanged():
    result = resolve_multi_turn("what's in the corridor?", [])
    assert result.rewritten_query == "what's in the corridor?"
    assert result.made_changes is False


def test_query_with_no_references_passes_through():
    history = [_turn(0, "tell me about hole PLS-22-08", mentions=[_hole(0, "PLS-22-08")])]
    result = resolve_multi_turn("what's the porosity of unit 4?", history)
    assert result.rewritten_query == "what's the porosity of unit 4?"
    assert result.made_changes is False
    assert result.overall_confidence == 1.0


# ---------------------------------------------------------------------------
# Possessive pronoun: "its" / "their"
# ---------------------------------------------------------------------------


def test_its_resolves_to_latest_hole_with_possessive():
    history = [
        _turn(0, "tell me about hole PLS-22-08", mentions=[_hole(0, "PLS-22-08")]),
    ]
    result = resolve_multi_turn("what are ITS top assays?", history)
    assert "PLS-22-08's top assays" in result.rewritten_query
    assert result.made_changes is True
    assert len(result.resolution_trace) == 1
    step = result.resolution_trace[0]
    assert step.kind == "pronoun"
    assert step.resolved_to == "PLS-22-08"
    assert step.source_turn_index == 0


def test_their_resolves_same_as_its():
    history = [_turn(0, "x", mentions=[_hole(0, "DDH-100")])]
    result = resolve_multi_turn("what are THEIR depths?", history)
    assert "DDH-100's depths" in result.rewritten_query


# ---------------------------------------------------------------------------
# Nominative pronoun: "it" / "they" / "that"
# ---------------------------------------------------------------------------


def test_it_resolves_to_latest_hole_without_possessive():
    history = [_turn(0, "x", mentions=[_hole(0, "PLS-22-08")])]
    result = resolve_multi_turn("how deep is IT?", history)
    assert result.rewritten_query == "how deep is PLS-22-08?"


def test_they_resolves_to_latest_hole():
    history = [_turn(0, "x", mentions=[_hole(0, "DDH-001")])]
    result = resolve_multi_turn("when were THEY drilled?", history)
    assert "DDH-001" in result.rewritten_query


# ---------------------------------------------------------------------------
# Demonstratives — entity-typed phrases
# ---------------------------------------------------------------------------


def test_the_same_hole_resolves_to_latest_hole():
    history = [
        _turn(0, "tell me about PLS-22-08", mentions=[_hole(0, "PLS-22-08")]),
        _turn(1, "ok", role="assistant"),
        _turn(2, "what about DDH-1234", mentions=[_hole(2, "DDH-1234")]),
    ]
    result = resolve_multi_turn(
        "what's the lithology log for THE SAME HOLE?", history,
    )
    # Most-recent hole is DDH-1234 (turn 2).
    assert "DDH-1234" in result.rewritten_query
    assert "the same hole" not in result.rewritten_query.lower()


def test_this_hole_resolves_to_latest_hole():
    history = [_turn(0, "x", mentions=[_hole(0, "PLS-22-08")])]
    result = resolve_multi_turn("show me THIS HOLE in 3D", history)
    assert "PLS-22-08" in result.rewritten_query


def test_those_assays_resolves_to_latest_hole():
    """'Those assays' implicitly belongs to the latest hole."""
    history = [_turn(0, "x", mentions=[_hole(0, "BG21-001")])]
    result = resolve_multi_turn("filter THOSE ASSAYS to > 1 g/t", history)
    assert "BG21-001" in result.rewritten_query


def test_the_same_property_resolves_to_latest_property():
    history = [
        _turn(0, "tell me about", mentions=[_property_mention(0, "Crackingstone")]),
        _turn(1, "ok", role="assistant"),
    ]
    result = resolve_multi_turn(
        "what reports cover THE SAME PROPERTY?", history,
    )
    assert "Crackingstone" in result.rewritten_query


# ---------------------------------------------------------------------------
# Comparative: "the previous one" / "the first one"
# ---------------------------------------------------------------------------


def test_the_previous_one_walks_back_one():
    history = [
        _turn(0, "x", mentions=[_hole(0, "PLS-22-08")]),
        _turn(1, "ok", role="assistant"),
        _turn(2, "y", mentions=[_hole(2, "DDH-1234")]),
    ]
    # Latest is DDH-1234; "the previous one" should resolve to that.
    result = resolve_multi_turn(
        "compare these to THE PREVIOUS ONE", history,
    )
    # walk_back=1 = the most recent mention.
    assert "DDH-1234" in result.rewritten_query


def test_the_first_one_walks_to_oldest():
    history = [
        _turn(0, "x", mentions=[_hole(0, "PLS-22-08")]),
        _turn(2, "y", mentions=[_hole(2, "DDH-1234")]),
    ]
    result = resolve_multi_turn("show me THE FIRST ONE", history)
    # walk_back=-1 sentinel → oldest mention = PLS-22-08
    assert "PLS-22-08" in result.rewritten_query


# ---------------------------------------------------------------------------
# Multi-step composition
# ---------------------------------------------------------------------------


def test_pronoun_and_demonstrative_compose_in_one_query():
    history = [
        _turn(0, "x", mentions=[_hole(0, "PLS-22-08")]),
    ]
    # Two references: "its" (pronoun, possessive) + "the same hole"
    # (demonstrative). Both should resolve to PLS-22-08.
    result = resolve_multi_turn(
        "show ITS top assays and the lithology for THE SAME HOLE",
        history,
    )
    assert "PLS-22-08's top assays" in result.rewritten_query
    assert "PLS-22-08" in result.rewritten_query
    # Both substitutions are recorded.
    assert len(result.resolution_trace) >= 2


# ---------------------------------------------------------------------------
# Resolution recency rules
# ---------------------------------------------------------------------------


def test_pronoun_resolves_to_most_recent_compatible_entity():
    """Two holes in history; 'it' picks the more recent one."""
    history = [
        _turn(0, "x", mentions=[_hole(0, "PLS-22-08")]),
        _turn(2, "y", mentions=[_hole(2, "DDH-1234")]),
    ]
    result = resolve_multi_turn("what's its TD?", history)
    assert "DDH-1234" in result.rewritten_query
    assert "PLS-22-08" not in result.rewritten_query


def test_type_specific_recency_for_pronouns():
    """'this' biases to property type; 'it' biases to hole. With both
    in history, each pronoun picks its preferred type."""
    history = [
        _turn(0, "x", mentions=[
            _property_mention(0, "Crackingstone"),
            _hole(0, "PLS-22-08"),
        ]),
    ]
    # 'this' biases to property → Crackingstone
    result_this = resolve_multi_turn("tell me about THIS deposit", history)
    assert "Crackingstone" in result_this.rewritten_query
    # 'it' biases to hole → PLS-22-08
    result_it = resolve_multi_turn("what's ITS depth?", history)
    assert "PLS-22-08" in result_it.rewritten_query


# ---------------------------------------------------------------------------
# Unresolved references
# ---------------------------------------------------------------------------


def test_unresolved_pronoun_lowers_confidence():
    """When 'it' has no entity to resolve to, the query stays as-is
    but overall_confidence drops below 1.0."""
    # History has NO entities at all.
    history = [
        ConversationTurn(
            turn_index=0, role="user", text="generic question without entities",
        ),
    ]
    result = resolve_multi_turn("but what about IT?", history)
    # No substitution happened (lower-case "it" in pattern is still
    # the verbatim word — see what comes back).
    assert result.overall_confidence < 1.0


def test_unresolved_demonstrative_lowers_confidence():
    history = [_turn(0, "no entities here")]
    result = resolve_multi_turn(
        "what about THE SAME HOLE?", history,
    )
    assert "the same hole" in result.rewritten_query.lower()
    assert result.overall_confidence < 1.0


# ---------------------------------------------------------------------------
# Pure-function invariant
# ---------------------------------------------------------------------------


def test_pure_function_does_not_mutate_history():
    history = [
        _turn(0, "tell me about PLS-22-08", mentions=[_hole(0, "PLS-22-08")]),
    ]
    before_text = history[0].text
    before_mentions = list(history[0].entity_mentions)
    _ = resolve_multi_turn("what are its assays?", history)
    assert history[0].text == before_text
    assert list(history[0].entity_mentions) == before_mentions


# ---------------------------------------------------------------------------
# Fallback: missing entity_mentions extracted from text
# ---------------------------------------------------------------------------


def test_resolver_falls_back_to_text_extraction_when_mentions_empty():
    """If history turns have empty entity_mentions, the resolver
    extracts them from text using extract_entity_mentions."""
    history = [
        ConversationTurn(
            turn_index=0,
            role="user",
            text="tell me about hole PLS-22-08 and DDH-1234",
            entity_mentions=(),  # empty — resolver should backfill
        ),
    ]
    result = resolve_multi_turn("what are ITS depths?", history)
    # Backfill should find PLS-22-08 or DDH-1234 and substitute.
    assert (
        "PLS-22-08" in result.rewritten_query
        or "DDH-1234" in result.rewritten_query
    )


# ---------------------------------------------------------------------------
# extract_entity_mentions
# ---------------------------------------------------------------------------


def test_extract_entity_mentions_finds_hole_id_patterns():
    text = "Compare hole PLS-22-08 to DDH-1234 in the same property."
    mentions = extract_entity_mentions(text, turn_index=0)
    surfaces = [m.surface_form for m in mentions if m.entity_type == "hole"]
    assert any("PLS-22-08" in s for s in surfaces)
    assert any("DDH-1234" in s for s in surfaces)


def test_extract_entity_mentions_finds_property_names():
    text = "What's the geology of the Crackingstone Property?"
    mentions = extract_entity_mentions(text, turn_index=0)
    prop_surfaces = [m.surface_form for m in mentions if m.entity_type == "property"]
    assert "Crackingstone" in prop_surfaces


def test_extract_entity_mentions_dedupes_repeats():
    text = "PLS-22-08 again — let's check PLS-22-08 once more."
    mentions = extract_entity_mentions(text, turn_index=0)
    # Same surface form appears only once in the output.
    assert len([m for m in mentions if m.surface_form == "PLS-22-08"]) == 1


def test_extract_entity_mentions_empty_for_neutral_text():
    text = "What's the deepest deposit in the area?"
    mentions = extract_entity_mentions(text, turn_index=0)
    # 'deposit' alone isn't a property (needs a title-case word before it).
    assert mentions == []


# ---------------------------------------------------------------------------
# Step source-turn tracking
# ---------------------------------------------------------------------------


def test_resolution_step_carries_source_turn_index():
    history = [
        _turn(0, "x", mentions=[_hole(0, "OLD-HOLE")]),
        _turn(5, "y", mentions=[_hole(5, "NEW-HOLE")]),
    ]
    result = resolve_multi_turn("what's its depth?", history)
    assert len(result.resolution_trace) == 1
    assert result.resolution_trace[0].source_turn_index == 5
    assert result.resolution_trace[0].resolved_to == "NEW-HOLE"


# ---------------------------------------------------------------------------
# Made-changes property
# ---------------------------------------------------------------------------


def test_made_changes_is_true_when_substitution_happens():
    history = [_turn(0, "x", mentions=[_hole(0, "PLS-22-08")])]
    result = resolve_multi_turn("what's ITS depth?", history)
    assert result.made_changes is True


def test_made_changes_is_false_when_query_passes_through():
    history = [_turn(0, "x", mentions=[_hole(0, "PLS-22-08")])]
    result = resolve_multi_turn("what's the porosity of unit 4?", history)
    assert result.made_changes is False
