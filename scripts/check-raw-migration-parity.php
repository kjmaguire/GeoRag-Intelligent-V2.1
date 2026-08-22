<?php

declare(strict_types=1);

/**
 * Fail when database/raw/ declares an object the migration chain never creates.
 *
 * GeoRAG has two DDL layers. `database/migrations/` is run by CD and is in
 * sync with Azure. `database/raw/` is applied by hand locally and by ci.yml
 * against a throwaway Postgres — cd.yml has no equivalent step, so on Azure
 * it has never run. Nineteen tables and fourteen functions exist ONLY in raw
 * SQL, and live code queries every one of them.
 *
 * Nobody noticed for months because nothing compared the two layers. This
 * does. The existing gap is baselined in raw-parity-baseline.txt so the gate
 * can land today; anything NEW fails the build. Shrinking the baseline is the
 * work — the file is the to-do list, and it can only get smaller.
 *
 * Usage:  php scripts/check-raw-migration-parity.php [--update-baseline]
 */
const RAW_DIR = __DIR__.'/../database/raw';
const MIGRATIONS_DIR = __DIR__.'/../database/migrations';
const BASELINE = __DIR__.'/raw-parity-baseline.txt';

/** Directories that are history or scratch, not deployable DDL. */
const EXCLUDED_DIRS = ['_archive', '_adhoc'];

function rawSqlFiles(): array
{
    $out = [];
    $it = new RecursiveIteratorIterator(new RecursiveDirectoryIterator(RAW_DIR));
    foreach ($it as $file) {
        if (! $file->isFile() || $file->getExtension() !== 'sql') {
            continue;
        }
        $path = str_replace(DIRECTORY_SEPARATOR, '/', $file->getPathname());
        foreach (EXCLUDED_DIRS as $skip) {
            if (str_contains($path, '/'.$skip.'/')) {
                continue 2;
            }
        }
        $out[] = $path;
    }
    sort($out);

    return $out;
}

/** @return array<string, string> qualified object name => declaring file */
function declaredObjects(array $files): array
{
    $found = [];

    foreach ($files as $path) {
        $sql = (string) file_get_contents($path);
        $short = 'raw/'.ltrim(substr($path, strrpos($path, '/database/raw/') + 14), '/');

        preg_match_all(
            '/CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-z_]+\.[a-z_0-9]+)/i',
            $sql,
            $tables,
        );
        foreach ($tables[1] as $name) {
            $found['table '.strtolower($name)] ??= $short;
        }

        preg_match_all(
            '/CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+([a-z_]+\.[a-z_0-9]+)/i',
            $sql,
            $functions,
        );
        foreach ($functions[1] as $name) {
            $found['function '.strtolower($name)] ??= $short;
        }
    }

    ksort($found);

    return $found;
}

function migrationCorpus(): string
{
    $corpus = '';
    foreach (glob(MIGRATIONS_DIR.'/*.php') ?: [] as $path) {
        $corpus .= (string) file_get_contents($path);
    }

    return strtolower($corpus);
}

$objects = declaredObjects(rawSqlFiles());
$corpus = migrationCorpus();

$missing = [];
foreach ($objects as $key => $file) {
    [, $name] = explode(' ', $key, 2);
    if (! str_contains($corpus, $name)) {
        $missing[$key] = $file;
    }
}

if (in_array('--update-baseline', $argv, true)) {
    $lines = array_map(
        static fn (string $k, string $f): string => $k.'  # '.$f,
        array_keys($missing),
        array_values($missing),
    );
    file_put_contents(BASELINE, implode('
', $lines).'
');
    printf('Baseline rewritten with %d known gap(s).
', count($missing));
    exit(0);
}

$baseline = [];
if (is_file(BASELINE)) {
    foreach (file(BASELINE, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES) ?: [] as $line) {
        $line = trim(preg_replace('/#.*$/', '', $line) ?? '');
        if ($line !== '') {
            $baseline[$line] = true;
        }
    }
}

$new = array_diff_key($missing, $baseline);
$fixed = array_diff_key($baseline, $missing);

foreach ($fixed as $key => $_) {
    echo "CLOSED    {$key} — now created by a migration. Remove it from the baseline.
";
}

if ($new === []) {
    printf(
        'raw/migration parity OK — %d object(s) still baselined as raw-only.
',
        count($baseline) - count($fixed),
    );
    exit($fixed === [] ? 0 : 1);
}

echo '
Raw SQL declares objects that no migration creates, so they will never
';
echo 'reach Azure — CD runs `artisan migrate` and nothing else:

';
foreach ($new as $key => $file) {
    echo "  NEW  {$key}
       declared in {$file}
";
}
echo '
Either add it to the migration chain, or — if it is genuinely
';
echo 'deploy-time raw SQL — add the file to database/raw/manifest.json so
';
echo '`php artisan db:apply-raw` carries it.
';

exit(1);
