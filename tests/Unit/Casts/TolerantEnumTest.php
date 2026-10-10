<?php

declare(strict_types=1);

namespace Tests\Unit\Casts;

use App\Casts\TolerantEnum;
use App\Enums\CollarStatus;
use App\Enums\HoleType;
use App\Models\Collar;
use InvalidArgumentException;
use stdClass;
use Tests\TestCase;
use ValueError;

/**
 * Reading a collar must never be able to take an endpoint down.
 *
 * Casting `hole_type` / `status` straight to their enums made every
 * out-of-vocabulary value throw on ACCESS, and CollarResource reads both while
 * serialising — so one ingested row answered 500 for every page of the collar
 * list that contained it. The ingestion stores the file's own words ("DDH",
 * "Core", "Closed", "completed"; silver_row_guard.py), so these are the ordinary
 * case, not an edge. Same shape as TolerantSurveyMethodTest.
 *
 * Extends the framework TestCase: the cast logs through the Log facade on the
 * degrade path, and a facade with no container throws.
 */
final class TolerantEnumTest extends TestCase
{
    private function holeType(): TolerantEnum
    {
        return new TolerantEnum(HoleType::class);
    }

    public function test_a_vocabulary_value_casts_to_its_case(): void
    {
        foreach (HoleType::cases() as $case) {
            $this->assertSame($case, $this->holeType()->get(new Collar, 'hole_type', $case->value, []));
        }

        foreach (CollarStatus::cases() as $case) {
            $this->assertSame(
                $case,
                (new TolerantEnum(CollarStatus::class))->get(new Collar, 'status', $case->value, []),
            );
        }
    }

    public function test_the_words_a_collar_file_actually_uses_do_not_throw(): void
    {
        // Straight from the ingestion's habits: a drilling code, a plain word,
        // a lower-case variant of a vocabulary value, and the unknown marker.
        foreach (['DDH', 'Core', 'diamond', 'RC '] as $word) {
            $this->assertNull($this->holeType()->get(new Collar, 'hole_type', $word, []), $word);
        }

        foreach (['Closed', 'completed', 'COMPLETED'] as $word) {
            $this->assertNull(
                (new TolerantEnum(CollarStatus::class))->get(new Collar, 'status', $word, []),
                $word,
            );
        }
    }

    public function test_null_and_empty_are_null(): void
    {
        $this->assertNull($this->holeType()->get(new Collar, 'hole_type', null, []));
        $this->assertNull($this->holeType()->get(new Collar, 'hole_type', '', []));
    }

    public function test_the_vocabularies_are_unchanged(): void
    {
        // The whole point of the cast is that the vocabularies did NOT move
        // (CLAUDE.md rule 6). If someone widens one later this fails, and they
        // can delete the cast deliberately rather than leaving both.
        $this->assertSame(
            ['Diamond', 'RC', 'RAB', 'Rotary', 'Percussion', 'Auger', 'exploration', 'unknown'],
            array_map(fn (HoleType $c) => $c->value, HoleType::cases()),
        );
        $this->assertSame(
            ['Active', 'Completed', 'Abandoned', 'active', 'In Progress', 'Planned', 'unknown'],
            array_map(fn (CollarStatus $c) => $c->value, CollarStatus::cases()),
        );
    }

    public function test_writing_an_out_of_vocabulary_value_still_fails_loudly(): void
    {
        // Reads degrade because the rows already exist. A write is the
        // application's own and must stay inside the vocabulary — the same
        // rule StoreCollarRequest enforces with Rule::enum.
        $this->expectException(ValueError::class);
        $this->holeType()->set(new Collar, 'hole_type', 'DDH', []);
    }

    public function test_writing_a_vocabulary_value_returns_its_backing_string(): void
    {
        $this->assertSame('Diamond', $this->holeType()->set(new Collar, 'hole_type', HoleType::Diamond, []));
        $this->assertSame('RC', $this->holeType()->set(new Collar, 'hole_type', 'RC', []));
        $this->assertNull($this->holeType()->set(new Collar, 'hole_type', null, []));
    }

    public function test_it_will_not_wrap_something_that_is_not_a_backed_enum(): void
    {
        $this->expectException(InvalidArgumentException::class);
        new TolerantEnum(stdClass::class);
    }

    public function test_the_collar_model_is_wired_through_it(): void
    {
        // A straight enum cast would throw here; the model must not.
        $collar = (new Collar)->setRawAttributes(['hole_type' => 'DDH', 'status' => 'Closed'], true);

        $this->assertNull($collar->hole_type);
        $this->assertNull($collar->status);
        $this->assertSame('DDH', $collar->getRawOriginal('hole_type'));
        $this->assertSame('Closed', $collar->getRawOriginal('status'));

        $known = (new Collar)->setRawAttributes(['hole_type' => 'RC', 'status' => 'Active'], true);
        $this->assertSame(HoleType::RC, $known->hole_type);
        $this->assertSame(CollarStatus::Active, $known->status);
    }
}
