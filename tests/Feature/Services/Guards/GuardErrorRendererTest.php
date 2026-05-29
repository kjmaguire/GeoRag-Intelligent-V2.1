<?php

declare(strict_types=1);

namespace Tests\Feature\Services\Guards;

use App\Services\Guards\GuardErrorRenderer;
use Tests\TestCase;

/**
 * Plan §4b — GuardErrorRenderer behaviour against lang/en/guard_errors.php.
 *
 * Feature test (not unit) because we exercise Laravel's __() helper,
 * which needs the framework's translation layer booted. The catalog
 * file is `lang/en/guard_errors.php` (PHP return array — Laravel 11+
 * uses .php for nested __('guard_errors.X') lookups; the prior .json
 * variant has been removed).
 */
final class GuardErrorRendererTest extends TestCase
{
    private GuardErrorRenderer $renderer;

    protected function setUp(): void
    {
        parent::setUp();
        $this->renderer = new GuardErrorRenderer;
    }

    public function test_renders_no_evidence_found_with_canonical_message(): void
    {
        $rendered = $this->renderer->render('NO_EVIDENCE_FOUND');

        $this->assertStringContainsString(
            "I couldn't find anything",
            $rendered,
            'Expected the canonical first-person template from lang/en/guard_errors.json',
        );
    }

    public function test_renders_entity_not_found_with_placeholders(): void
    {
        $rendered = $this->renderer->render('ENTITY_NOT_FOUND', [
            'entity' => 'Rowan',
            'suggested_aliases' => 'WRLG Rowan, West Red Lake Rowan',
        ]);

        $this->assertStringContainsString('Rowan', $rendered);
        $this->assertStringContainsString('WRLG Rowan', $rendered);
    }

    public function test_entity_not_found_degrades_when_aliases_empty(): void
    {
        // suggested_aliases empty → renderer picks _NO_ALIASES variant.
        $rendered = $this->renderer->render('ENTITY_NOT_FOUND', [
            'entity' => 'Rowan',
            'suggested_aliases' => '',
        ]);

        // The _NO_ALIASES template does NOT carry "Did you mean: …".
        $this->assertStringContainsString('Rowan', $rendered);
        $this->assertStringNotContainsString('Did you mean', $rendered);
        $this->assertStringContainsString(
            'may not appear in the ingested documents',
            $rendered,
        );
    }

    public function test_entity_not_found_degrades_when_aliases_null(): void
    {
        $rendered = $this->renderer->render('ENTITY_NOT_FOUND', [
            'entity' => 'Rowan',
        ]);

        $this->assertStringNotContainsString('Did you mean', $rendered);
    }

    public function test_conflicting_sources_renders_neutral_when_no_authority(): void
    {
        $rendered = $this->renderer->render('CONFLICTING_SOURCES', [
            'document_a' => 'NI 43-101 (2024)',
            'document_b' => 'Fact sheet (2024)',
            'value_a' => '1.18 Moz Au',
            'value_b' => '1.1 Moz Au',
            'interpretation_or_rounding' => 'rounding',
        ]);

        $this->assertStringContainsString('NI 43-101', $rendered);
        $this->assertStringContainsString('Fact sheet', $rendered);
        // Neutral variant does NOT carry "The current source is …".
        $this->assertStringNotContainsString('current source is', $rendered);
    }

    public function test_conflicting_sources_picks_authority_variant_when_supersession_known(): void
    {
        $rendered = $this->renderer->render('CONFLICTING_SOURCES', [
            'document_a' => 'NI 43-101 (2024)',
            'document_b' => 'NI 43-101 (2021)',
            'value_a' => '1.18 Moz Au',
            'value_b' => '0.9 Moz Au',
            'interpretation_or_rounding' => 'updated estimate',
            'authoritative_doc' => 'NI 43-101 (2024)',
        ]);

        $this->assertStringContainsString('current source is', $rendered);
        $this->assertStringContainsString('NI 43-101 (2024)', $rendered);
    }

    public function test_unknown_code_falls_back_to_unsupported_query_type(): void
    {
        $rendered = $this->renderer->render('NOT_A_REAL_CODE');

        $this->assertStringContainsString(
            'outside what I can answer',
            $rendered,
        );
    }

    public function test_render_many_returns_one_string_per_code(): void
    {
        $rendered = $this->renderer->renderMany(
            ['NO_EVIDENCE_FOUND', 'CITATION_INCOMPLETE'],
        );

        $this->assertCount(2, $rendered);
        $this->assertStringContainsString("I couldn't find anything", $rendered[0]);
        $this->assertStringContainsString("don't have enough citations", $rendered[1]);
    }

    public function test_is_known_code_recognises_all_plan_4b_codes(): void
    {
        $canonical = [
            'NO_EVIDENCE_FOUND', 'ENTITY_NOT_FOUND', 'AMBIGUOUS_HOLE_ID',
            'AMBIGUOUS_FORMATION_NAME', 'AMBIGUOUS_PROPERTY_NAME',
            'OVER_FILTERED_QUERY', 'SPATIAL_QUERY_EMPTY',
            'SPATIAL_CRS_MISMATCH', 'GRAPH_PATH_NOT_FOUND',
            'NUMERIC_GROUNDING_FAILED', 'CITATION_INCOMPLETE',
            'CONFLICTING_SOURCES', 'MISSING_DEPTH_INTERVAL',
            'MISSING_ASSAY_UNITS', 'SOURCE_SCOPE_VIOLATION',
            'UNSUPPORTED_QUERY_TYPE',
        ];

        foreach ($canonical as $code) {
            $this->assertTrue(
                $this->renderer->isKnownCode($code),
                "Expected '{$code}' to be a known plan §4b code",
            );
        }
    }

    public function test_is_known_code_rejects_unknown_codes(): void
    {
        $this->assertFalse($this->renderer->isKnownCode('NOT_A_REAL_CODE'));
        $this->assertFalse($this->renderer->isKnownCode(''));
        $this->assertFalse($this->renderer->isKnownCode('no_evidence_found')); // case-sensitive
    }
}
