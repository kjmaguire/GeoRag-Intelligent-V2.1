<?php

declare(strict_types=1);

/**
 * Fail when a DDL file turns RLS on for a table without also forcing it.
 *
 * `ALTER TABLE ... ENABLE ROW LEVEL SECURITY` does not apply to the table's
 * OWNER. Only `FORCE` does. Production migrates and applies `database/raw/`
 * as `georag`, which owns every table it creates, so an ENABLE-only table
 * has no tenant isolation at all against that role.
 *
 * WHY THIS IS A STATIC CHECK AND NOT ONLY A TEST
 *     WorkspaceRlsCoverageTest::test_every_rls_enabled_table_is_forced
 *     already asserts the strong invariant — zero ENABLE-without-FORCE — but
 *     it asserts it against the TEST cluster, which RefreshDatabase builds
 *     from `database/migrations/` alone. `database/raw/` is never applied
 *     there, so no table that layer creates is visible to it. This check
 *     reads both trees off disk, so it covers the half the test cannot see,
 *     and it fails at review time rather than after someone runs migrations.
 *
 * WHAT THE BASELINE IS, AND WHAT IT IS NOT
 *     The 26 baselined entries are NOT open gaps. They are closed at runtime
 *     by one of two things, and the file records which:
 *
 *       - migrations dated before 2026-08-24, closed by
 *         2026_08_24_010000_force_row_level_security_on_all_rls_enabled_tables,
 *         a catalog-driven sweep that forces whatever the cluster has
 *         ENABLE-only at that moment;
 *       - `database/raw/` files that are not in `database/raw/manifest.json`,
 *         so `db:apply-raw` never runs them and they create nothing.
 *
 *     So this is not a debt register to pay down. It exists because that
 *     sweep is a one-time migration: it will not run again, and a NEW file
 *     written in the old style would be closed by nothing. The baseline is
 *     the line between "covered by the sweep" and "written after it".
 *
 *     A baselined entry that no longer has the gap fails too. A stale
 *     exemption is how the next one hides.
 *
 * SCOPE
 *     File-local, deliberately. A file that ENABLEs and a different file that
 *     FORCEs is exactly the shape the sweep uses, and treating the whole tree
 *     as one text would make this check blind to precisely what it is for.
 *
 * Usage:  php scripts/check-rls-force-parity.php
 */

const BASELINE = __DIR__.'/rls-force-baseline.txt';

/**
 * Tables a file switches RLS on for, and tables it forces.
 *
 * Table names are kept verbatim — `{$qualified}`, `silver.%I` and a literal
 * name are all just tokens. What matters is whether the SAME token appears
 * in both statements, which is true for an interpolated loop variable
 * exactly when the loop does both.
 *
 * @return array{enable: list<string>, force: list<string>}
 */
function rlsStatements(string $sql): array
{
    $enable = [];
    $force = [];

    preg_match_all(
        '/ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(\S+)\s+ENABLE\s+ROW\s+LEVEL\s+SECURITY/i',
        $sql, $m
    );
    foreach ($m[1] as $name) {
        $enable[] = strtolower(trim($name));
    }

    preg_match_all(
        '/ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(\S+)\s+FORCE\s+ROW\s+LEVEL\s+SECURITY/i',
        $sql, $m
    );
    foreach ($m[1] as $name) {
        $force[] = strtolower(trim($name));
    }

    return ['enable' => $enable, 'force' => $force];
}

/** @return list<string> Every DDL file, repo-relative, sorted. */
function ddlFiles(string $repo): array
{
    $found = [];
    foreach ([
        $repo.'/database/migrations' => 'php',
        $repo.'/database/raw' => 'sql',
    ] as $root => $extension) {
        if (! is_dir($root)) {
            continue;
        }
        $walker = new RecursiveIteratorIterator(
            new RecursiveDirectoryIterator($root, FilesystemIterator::SKIP_DOTS)
        );
        foreach ($walker as $file) {
            if ($file->isFile() && $file->getExtension() === $extension) {
                $found[] = str_replace($repo.'/', '', $file->getPathname());
            }
        }
    }
    sort($found);

    return $found;
}

/** @return list<string> Non-comment, non-blank baseline lines. */
function baselineEntries(): array
{
    if (! is_file(BASELINE)) {
        fwrite(STDERR, 'Missing '.BASELINE."\n");
        exit(2);
    }
    $entries = [];
    foreach (file(BASELINE, FILE_IGNORE_NEW_LINES) ?: [] as $line) {
        $line = trim($line);
        if ($line !== '' && ! str_starts_with($line, '#')) {
            $entries[] = $line;
        }
    }

    return $entries;
}

$repo = dirname(__DIR__);
$baseline = baselineEntries();

/** @var list<string> $gaps `path::table` for every ENABLE without a FORCE. */
$gaps = [];
foreach (ddlFiles($repo) as $relative) {
    $contents = @file_get_contents($repo.'/'.$relative);
    if ($contents === false) {
        continue;
    }
    $statements = rlsStatements($contents);
    foreach (array_unique(array_diff($statements['enable'], $statements['force'])) as $table) {
        $gaps[] = "{$relative}::{$table}";
    }
}
sort($gaps);

$new = array_values(array_diff($gaps, $baseline));
$stale = array_values(array_diff($baseline, $gaps));

if ($new === [] && $stale === []) {
    printf(
        "RLS force parity: %d baselined entr(ies), no new ENABLE-without-FORCE.\n",
        count($baseline)
    );
    exit(0);
}

$status = 0;

if ($new !== []) {
    $status = 1;
    echo "\nThese switch RLS on without forcing it, and are not baselined:\n\n";
    foreach ($new as $entry) {
        echo "  {$entry}\n";
    }
    echo "\nENABLE ROW LEVEL SECURITY does not apply to the table's owner, and\n";
    echo "production migrates as georag, which owns every table it creates. Add\n";
    echo "the matching statement in the same file:\n\n";
    echo "  ALTER TABLE <table> FORCE ROW LEVEL SECURITY;\n\n";
    echo "The 2026_08_24_010000 catalog sweep does NOT cover this — it is a\n";
    echo "one-time migration that has already run everywhere it will run.\n";
}

if ($stale !== []) {
    $status = 1;
    echo "\nThese are baselined but no longer have the gap — delete the lines:\n\n";
    foreach ($stale as $entry) {
        echo "  {$entry}\n";
    }
    echo "\nA baseline entry that has been closed and left in the file is how\n";
    echo "the next real one hides behind it.\n";
}

exit($status);
