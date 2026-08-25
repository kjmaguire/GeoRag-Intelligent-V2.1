<?php

declare(strict_types=1);

namespace Tests\Unit\Casts;

use App\Casts\TolerantSurveyMethod;
use App\Enums\SurveyMethod;
use App\Models\Survey;
use Tests\TestCase;
use ValueError;

/**
 * Reading a survey must never be able to take an endpoint down.
 *
 * Casting `survey_method` straight to the enum made every out-of-vocabulary
 * value throw on ACCESS, and CollarController::show catches Throwable — so
 * one ingested survey row answered 500 for every collar in the project.
 *
 * The values that do it are not exotic. `_SURVEY_METHOD_DEFAULT = 'unknown'`
 * in ingest_tabular.py is written for every station whose sheet named no
 * instrument, and the Discover-trace path writes 'desurveyed_trace'. Both are
 * provenance rather than instrument families, which is why the fix is a total
 * READ rather than a wider vocabulary — §04e's list is closed and CLAUDE.md
 * rule 6 reserves it for the SME.
 *
 * Extends the framework TestCase rather than the bare PHPUnit one: the
 * cast logs through the Log facade on the degrade path, and a facade with
 * no container throws "A facade root has not been set" — which would fail
 * these tests for a reason that has nothing to do with what they assert.
 */
final class TolerantSurveyMethodTest extends TestCase
{
    private function cast(): TolerantSurveyMethod
    {
        return new TolerantSurveyMethod;
    }

    public function test_a_vocabulary_value_still_casts_to_the_enum(): void
    {
        foreach (SurveyMethod::cases() as $case) {
            $this->assertSame(
                $case,
                $this->cast()->get(new Survey, 'survey_method', $case->value, []),
            );
        }
    }

    public function test_the_ingestion_default_does_not_throw(): void
    {
        // The value that actually takes the endpoint down today.
        $this->assertNull(
            $this->cast()->get(new Survey, 'survey_method', 'unknown', []),
        );
    }

    public function test_a_desurveyed_trace_does_not_throw(): void
    {
        $this->assertNull(
            $this->cast()->get(new Survey, 'survey_method', 'desurveyed_trace', []),
        );
    }

    public function test_null_and_empty_are_null(): void
    {
        $this->assertNull($this->cast()->get(new Survey, 'survey_method', null, []));
        $this->assertNull($this->cast()->get(new Survey, 'survey_method', '', []));
    }

    public function test_the_enum_itself_is_unchanged(): void
    {
        // The whole point of the cast is that the vocabulary did NOT move.
        // If someone widens it later, this fails and they can delete the
        // cast deliberately rather than leaving both.
        $this->assertSame(
            ['Gyro', 'Magnetic', 'Multishot'],
            array_map(fn (SurveyMethod $m) => $m->value, SurveyMethod::cases()),
        );
    }

    public function test_writing_an_out_of_vocabulary_value_still_fails_loudly(): void
    {
        // Reads degrade because the rows already exist. Writes must not:
        // nothing in the app writes this column, so a write is a mistake and
        // should surface at the point of the mistake.
        $this->expectException(ValueError::class);
        $this->cast()->set(new Survey, 'survey_method', 'desurveyed_trace', []);
    }

    public function test_writing_a_vocabulary_value_returns_its_backing_string(): void
    {
        $this->assertSame(
            'Gyro',
            $this->cast()->set(new Survey, 'survey_method', SurveyMethod::Gyro, []),
        );
        $this->assertSame(
            'Multishot',
            $this->cast()->set(new Survey, 'survey_method', 'Multishot', []),
        );
    }
}
