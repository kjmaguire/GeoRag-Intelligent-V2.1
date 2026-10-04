<?php

declare(strict_types=1);

namespace Tests\Unit\Services;

use App\Services\IngestionSnapshot;
use App\Services\StorageService;
use Illuminate\Support\Facades\Cache;
use Illuminate\Support\Facades\Storage;
use Tests\TestCase;

/**
 * IngestionSnapshot runs on every 5 s poll, so its cost has to be bounded:
 * the bronze listing is one request per prefix (size and mtime come with the
 * listing), is capped newest-first, is rebuilt by one caller at a time, and
 * the payload says when it cut anything.
 *
 * No database: the listing and the pure assemble() half are exercised.
 */
final class IngestionSnapshotBoundsTest extends TestCase
{
    private const PROJECT = '11111111-2222-3333-4444-555555555555';

    protected function setUp(): void
    {
        parent::setUp();
        Storage::fake('s3-bronze');
        Cache::flush();
    }

    private function snapshot(): IngestionSnapshot
    {
        return app(IngestionSnapshot::class);
    }

    /**
     * @return list<array{key: string, filename: string, size_bytes: ?int, uploaded_at: ?string}>
     */
    private function listUploads(): array
    {
        $method = new \ReflectionMethod(IngestionSnapshot::class, 'listUploads');

        return $method->invoke($this->snapshot(), self::PROJECT);
    }

    public function test_the_listing_carries_size_and_upload_time_from_a_single_listing(): void
    {
        Storage::disk('s3-bronze')->put('reports/'.self::PROJECT.'/20260929_012744_a.pdf', 'twelve bytes');

        $uploads = $this->listUploads();

        $this->assertCount(1, $uploads);
        $this->assertSame('reports/'.self::PROJECT.'/20260929_012744_a.pdf', $uploads[0]['key']);
        $this->assertSame('20260929_012744_a.pdf', $uploads[0]['filename']);
        $this->assertSame(12, $uploads[0]['size_bytes']);
        $this->assertNotNull($uploads[0]['uploaded_at']);
    }

    public function test_the_listing_is_capped_and_flags_that_it_was_cut(): void
    {
        $disk = Storage::disk('s3-bronze');
        $total = IngestionSnapshot::MAX_ROWS_PER_SECTION + 25;
        for ($i = 0; $i < $total; $i++) {
            $disk->put('reports/'.self::PROJECT.'/'.sprintf('f%04d.pdf', $i), 'x');
        }

        $uploads = $this->listUploads();

        // MAX + 1: the extra row is how build() knows it cut something.
        $this->assertCount(IngestionSnapshot::MAX_ROWS_PER_SECTION + 1, $uploads);
    }

    public function test_assemble_reports_which_sections_were_truncated(): void
    {
        $none = $this->snapshot()->assemble([], [], []);
        $this->assertFalse($none['truncated']);
        $this->assertSame(['reports' => false, 'progress' => false, 'uploads' => false], $none['truncated_sections']);

        $cut = $this->snapshot()->assemble([], [], [], ['progress' => true]);
        $this->assertTrue($cut['truncated']);
        $this->assertSame(['reports' => false, 'progress' => true, 'uploads' => false], $cut['truncated_sections']);
    }

    public function test_a_second_call_inside_the_ttl_is_served_from_cache(): void
    {
        $disk = Storage::disk('s3-bronze');
        $disk->put('reports/'.self::PROJECT.'/a.pdf', 'x');
        $this->assertCount(1, $this->listUploads());

        // A new object lands, but the 8 s window has not elapsed.
        $disk->put('reports/'.self::PROJECT.'/b.pdf', 'x');

        $this->assertCount(1, $this->listUploads());
    }

    public function test_a_caller_that_loses_the_rebuild_race_is_served_the_stale_listing_not_a_second_listing(): void
    {
        $key = 'ingestion-runs:uploads:'.self::PROJECT;
        $stale = [['key' => 'reports/'.self::PROJECT.'/stale.pdf', 'filename' => 'stale.pdf', 'size_bytes' => 1, 'uploaded_at' => null]];
        Cache::put($key.':stale', $stale, 300);

        // Another worker is mid-rebuild and holds the lock.
        $held = Cache::lock($key.':lock', 30);
        $this->assertTrue($held->get());

        // The disk has a DIFFERENT object: if this caller listed it we would see it.
        Storage::disk('s3-bronze')->put('reports/'.self::PROJECT.'/fresh.pdf', 'x');

        // Zero wait: serve stale at once instead of waiting for the lock holder.
        $impatient = new IngestionSnapshot(app(StorageService::class), 0);
        $method = new \ReflectionMethod(IngestionSnapshot::class, 'listUploads');

        $this->assertSame($stale, $method->invoke($impatient, self::PROJECT));

        $held->release();
    }

    public function test_the_lock_is_released_after_a_rebuild(): void
    {
        Storage::disk('s3-bronze')->put('reports/'.self::PROJECT.'/a.pdf', 'x');
        $this->listUploads();

        $lock = Cache::lock('ingestion-runs:uploads:'.self::PROJECT.':lock', 5);
        $this->assertTrue($lock->get(), 'the rebuild lock must not outlive the rebuild');
        $lock->release();
    }
}
