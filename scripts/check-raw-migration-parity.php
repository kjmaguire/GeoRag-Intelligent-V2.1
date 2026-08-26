<?php

declare(strict_types=1);

/**
 * Fail when database/raw/ declares an object the migration chain never creates.
 *
 * GeoRAG has two DDL layers. `database/migrations/` is run by CD and is in
 * sync with Azure. `database/raw/` is applied by hand locally and by ci.yml
 * against a throwaway Postgres — cd.yml has no equivalent step, so on Azure
 * it has never run. Dozens of tables and functions exist ONLY in raw SQL,
 * and live code queries many of them.
 *
 * Nobody noticed for months because nothing compared the two layers. This
 * does. The existing gap is baselined in raw-parity-baseline.txt so the gate
 * can land today; anything NEW fails the build. Shrinking the baseline is the
 * work — the file is the to-do list, and it can only get smaller.
 *
 * "Managed by a migration" means a migration CREATES the object: a real
 * CREATE TABLE / CREATE FUNCTION statement (DB::statement or heredoc) or a
 * schema-qualified Schema::create()/->create() call. A migration that merely
 * ALTERs, GRANTs, attaches an RLS policy to, DROPs, or names an object in a
 * comment does NOT manage it. The first version of this gate used a substring
 * match over the concatenated migration text, and PR #188 demonstrated the
 * failure: a migration that only ALTERed silver.storage_tier_policy
 * force-closed its baseline entry, and eleven more raw-only objects were
 * never baselined at all because some migration mentioned their name — one
 * of them (silver.assays) purely because "silver.assays" is a substring of
 * "silver.assays_v2".
 *
 * Test-DB mirrors: the provision_*_for_test_db migrations gated on `pgsql`
 * run on Azure too, where raw SQL never ran — their CREATE TABLE IF NOT
 * EXISTS is what actually created those objects there, so they count as
 * managing the object's EXISTENCE (shape fidelity — partitions, triggers —
 * is beyond this gate's scope). Mirrors gated on `sqlite` create unqualified
 * names and never touch Azure; the qualified-name match below ignores them
 * for exactly that reason. Caveat: a sqlite-only migration that created a
 * schema-qualified name would be miscounted as managing the object — none
 * does today (only public_geo.sources, which raw SQL does not declare).
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

/**
 * Every object the migration chain CREATES, as "type schema.name" keys.
 *
 * Matches the three forms migrations actually use — raw DDL in
 * DB::statement()/heredocs (with or without IF NOT EXISTS, identifiers
 * optionally double-quoted) and the schema builder with a qualified name.
 * Deliberately does NOT match ALTER/GRANT/DROP/POLICY statements or comments:
 * naming an object is not managing it.
 *
 * @return array<string, true>
 */
function migrationCreatedObjects(): array
{
    $created = [];

    foreach (glob(MIGRATIONS_DIR.'/*.php') ?: [] as $path) {
        $src = (string) file_get_contents($path);

        preg_match_all(
            '/CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"?([a-z_]+)"?\s*\.\s*"?([a-z_0-9]+)"?/i',
            $src,
            $tables,
            PREG_SET_ORDER,
        );
        foreach ($tables as $m) {
            $created['table '.strtolower($m[1].'.'.$m[2])] = true;
        }

        preg_match_all(
            '/CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+"?([a-z_]+)"?\s*\.\s*"?([a-z_0-9]+)"?/i',
            $src,
            $functions,
            PREG_SET_ORDER,
        );
        foreach ($functions as $m) {
            $created['function '.strtolower($m[1].'.'.$m[2])] = true;
        }

        // Schema::create('silver.x', …) or Schema::connection(…)->create('silver.x', …)
        preg_match_all(
            '/(?:Schema::|->)\s*create\(\s*[\'"]([a-z_]+\.[a-z_0-9]+)[\'"]/i',
            $src,
            $builder,
        );
        foreach ($builder[1] as $name) {
            $created['table '.strtolower($name)] = true;
        }
    }

    return $created;
}

$objects = declaredObjects(rawSqlFiles());
$created = migrationCreatedObjects();

$missing = [];
foreach ($objects as $key => $file) {
    if (! isset($created[$key])) {
        $missing[$key] = $file;
    }
}

if (in_array('--update-baseline', $argv, true)) {
    // Preserve the hand-written leading comment block; regenerate the entries.
    $header = '';
    if (is_file(BASELINE)) {
        foreach (file(BASELINE, FILE_IGNORE_NEW_LINES) ?: [] as $line) {
            if (! str_starts_with($line, '#')) {
                break;
            }
            $header .= $line."\n";
        }
    }
    $lines = array_map(
        static fn (string $k, string $f): string => $k.'  # '.$f,
        array_keys($missing),
        array_values($missing),
    );
    file_put_contents(BASELINE, $header."\n".implode("\n", $lines)."\n");
    printf("Baseline rewritten with %d known gap(s).\n", count($missing));
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
    echo "CLOSED    {$key} — now created by a migration. Remove it from the baseline.\n";
}

if ($new === []) {
    printf(
        "raw/migration parity OK — %d object(s) still baselined as raw-only.\n",
        count($baseline) - count($fixed),
    );
    exit($fixed === [] ? 0 : 1);
}

echo "\nRaw SQL declares objects that no migration creates, so they will never\n";
echo "reach Azure — CD runs `artisan migrate` and nothing else:\n\n";
foreach ($new as $key => $file) {
    echo "  NEW  {$key}\n       declared in {$file}\n";
}
echo "\nEither add it to the migration chain, or — if it is genuinely\n";
echo "deploy-time raw SQL — add the file to database/raw/manifest.json so\n";
echo "`php artisan db:apply-raw` carries it.\n";

exit(1);
