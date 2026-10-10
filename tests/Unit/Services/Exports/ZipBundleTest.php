<?php

declare(strict_types=1);

namespace Tests\Unit\Services\Exports;

use App\Services\Exports\ZipBundle;
use PHPUnit\Framework\TestCase;
use RuntimeException;
use ZipArchive;

final class ZipBundleTest extends TestCase
{
    /** @var list<string> */
    private array $cleanup = [];

    protected function tearDown(): void
    {
        foreach ($this->cleanup as $path) {
            if (is_file($path)) {
                unlink($path);
            }
        }

        parent::tearDown();
    }

    private function temp(string $contents = ''): string
    {
        $path = tempnam(sys_get_temp_dir(), 'zipbundle_test_');
        file_put_contents($path, $contents);

        return $this->cleanup[] = $path;
    }

    public function test_it_writes_every_entry_under_its_name(): void
    {
        $zipPath = $this->temp();
        $a = $this->temp('alpha');
        $b = $this->temp('beta');

        ZipBundle::write($zipPath, ['one.csv' => $a, 'two.csv' => $b]);

        $zip = new ZipArchive;
        $this->assertTrue($zip->open($zipPath) === true);
        $this->assertSame('alpha', $zip->getFromName('one.csv'));
        $this->assertSame('beta', $zip->getFromName('two.csv'));
        $zip->close();
    }

    public function test_a_file_that_cannot_be_added_fails_loudly(): void
    {
        // addFile() answers `false` for a missing file rather than throwing;
        // ignoring it shipped an archive without the entry.
        $zipPath = $this->temp();

        $this->expectException(RuntimeException::class);
        $this->expectExceptionMessage('Cannot add missing.csv');

        ZipBundle::write($zipPath, ['missing.csv' => sys_get_temp_dir().'/zipbundle_does_not_exist_'.uniqid()]);
    }

    public function test_a_failure_part_way_leaves_nothing_for_the_destructor_to_trip_over(): void
    {
        // The first entry is queued fine, the second cannot be added. The
        // caller deletes the files next; the half-built archive must not go
        // looking for them afterwards.
        $zipPath = $this->temp();
        $good = $this->temp('fine');

        try {
            ZipBundle::write($zipPath, ['good.csv' => $good, 'missing.csv' => sys_get_temp_dir().'/zipbundle_gone_'.uniqid()]);
            $this->fail('the second entry cannot be added');
        } catch (RuntimeException) {
            if (is_file($good)) {
                unlink($good);
            }
        }

        $this->addToAssertionCount(1);
    }
}
