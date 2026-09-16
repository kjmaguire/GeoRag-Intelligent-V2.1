<?php

declare(strict_types=1);

/**
 * Fail when a tracked file carries something that looks like a real password.
 *
 * On 2026-08-21 the live dev-cluster Postgres password was found in 25 tracked
 * files of a PUBLIC repository. It got there deliberately: a handoff doc
 * recorded reading it out of the running container with
 * `docker exec georag-postgresql env` so the test config would "stay in sync".
 *
 * GitHub's own scanning did not catch it. Secret scanning and push protection
 * are both enabled on the repo, but `secret_scanning_non_provider_patterns` is
 * disabled — and a generic database password matches no provider pattern, so
 * nothing looked at it.
 *
 * This is the repo-side backstop. It is deliberately narrow: it looks at
 * password-shaped assignments and DSN credentials only, and it only complains
 * about values that look random. Placeholders are what belongs in the repo, so
 * they are allowed by name rather than by entropy — which also means adding a
 * new one is a visible, reviewable act.
 *
 * Usage:  php scripts/check-no-committed-secrets.php
 */

/** Values that are meant to be here. Keep this list short and boring. */
const ALLOWED = [
    'georag_dev_password',
    'georag_app_pw',
    'bootstrap_pw',
    'test_password',
    'ci_test_password',
    'georag',
    'password',
    'secret',
    'changeme',
    'postgres',
];

/** Paths where an example credential is the whole point. */
const SKIP_PATHS = [
    'scripts/check-no-committed-secrets.php',
    'composer.lock',
    'package-lock.json',
    'uv.lock',
];

/**
 * Does this look like a generated credential rather than a placeholder?
 *
 * The real one was `OMljaORhiA7RGQN3ilfemNWpezF9waU`: 31 characters, mixed
 * case, digits, no separators. A placeholder is words joined by underscores
 * or dashes, so requiring an absence of separators plus mixed case plus a
 * digit is enough to tell them apart without a Shannon-entropy calculation
 * nobody will be able to reason about when this fires.
 */
function looksGenerated(string $value): bool
{
    if (strlen($value) < 16) {
        return false;
    }
    if (str_contains($value, '_') || str_contains($value, '-') || str_contains($value, ' ')) {
        return false;
    }
    if (str_contains($value, '$') || str_contains($value, '{')) {
        return false;  // shell / compose interpolation, not a literal
    }

    // An unsigned JWT carries no secret by construction — the header says
    // `alg: none` and the whole thing base64-decodes to public claims. The
    // Hatchet dev token in the CI workflows is one of these.
    if (str_starts_with($value, 'eyJhbGciOiAibm9uZSI')) {
        return false;
    }

    return preg_match('/[a-z]/', $value) === 1
        && preg_match('/[A-Z]/', $value) === 1
        && preg_match('/[0-9]/', $value) === 1;
}

/** @return list<string> */
function trackedFiles(): array
{
    exec('git ls-files', $out, $code);
    if ($code !== 0) {
        fwrite(STDERR, "git ls-files failed — run this inside the repository.\n");
        exit(2);
    }

    return $out;
}

$patterns = [
    // KEY: value / KEY=value / "KEY" => "value"
    '/(?:PASSWORD|PASSWD|PWD|SECRET|TOKEN)[A-Z_]*\s*[:=>]+\s*[\'"]?([^\'"\s,;)]+)/i',
    // postgres://user:password@host, redis://…, amqp://…
    '#[a-z][a-z0-9+.-]*://[^:/@\s]+:([^@/\s]+)@#i',
    // ${NEO4J_PASSWORD:-24kNKWLbX20bgHEXAuMSGjCp228LIfUE} — a shell default.
    //
    // This needs its own pattern because the first one ALMOST matches and
    // that is worse than not matching at all. `[:=>]+` consumes the `:` of
    // `:-`, so the capture begins at the dash: `-24kNKWLb…}`. looksGenerated()
    // then sees a leading separator, concludes "placeholder", and passes it.
    // Two real credentials — a Neo4j password and a Redis password — sat in
    // tracked files under scripts/ behind exactly that near-miss, in a public
    // repo, while this check reported clean. Capture the value itself.
    // Digits belong in the name class: the first credential this caught
    // was NEO4J_PASSWORD, and `[A-Z_]*` cannot match the 4 in NEO4J.
    '/\$\{[A-Z0-9_]*(?:PASSWORD|PASSWD|PWD|SECRET|TOKEN)[A-Z0-9_]*:[-=]([^}\s]+)\}/i',
    // `redis-cli -a <value>` and `--requirepass <value>`.
    //
    // Every pattern above matches an ASSIGNMENT. A credential handed to a
    // program as an argument is not one, so none of them ever looked at
    //
    //     docker exec georag-redis redis-cli -a 'N2Wz…' --no-auth-warning
    //
    // which sat in scripts/phase0_wrapper_smoke.sh from 2026-08-25 until
    // 2026-09-15 while this checker reported clean across 3144 tracked files.
    // ee853f5 had swept two sibling occurrences and missed this shape twice,
    // because a `grep PASSWORD` finds assignments and this is not one.
    //
    // Interpolated forms stay quiet on their own merit rather than by
    // exception: `--requirepass "$REDIS_PASSWORD"` captures a value carrying
    // `$`, which looksGenerated() already rejects.
    '/\bredis-(?:cli|benchmark)\b[^\r\n]*?\s-a\s+[\'"]?([^\'"\s]+)/i',
    '/--requirepass[=\s]+[\'"]?([^\'"\s]+)/i',
];

$hits = [];
foreach (trackedFiles() as $path) {
    if (in_array($path, SKIP_PATHS, true) || ! is_file($path)) {
        continue;
    }
    $contents = @file_get_contents($path);
    if ($contents === false || ! mb_check_encoding($contents, 'UTF-8')) {
        continue;  // binary
    }

    foreach (preg_split('/\R/', $contents) ?: [] as $n => $line) {
        foreach ($patterns as $pattern) {
            if (preg_match_all($pattern, $line, $matches) === 0) {
                continue;
            }
            foreach ($matches[1] as $value) {
                if (in_array($value, ALLOWED, true) || ! looksGenerated($value)) {
                    continue;
                }
                $hits[] = sprintf('%s:%d  %s', $path, $n + 1, $value);
            }
        }
    }
}

if ($hits === []) {
    printf("no committed credentials found in %d tracked file(s).\n", count(trackedFiles()));
    exit(0);
}

echo "\nThese tracked files carry values that look like real credentials:\n\n";
foreach (array_unique($hits) as $hit) {
    echo "  {$hit}\n";
}
echo "\nThis repository was public until 2026-09-15, and git history outlives\n";
echo "the working tree either way. If any of these is a live credential:\n";
echo "  1. Rotate it. That is the load-bearing step — the value is in git\n";
echo "     history whether or not you remove it from the working tree.\n";
echo "  2. Replace it with a placeholder and add the placeholder to ALLOWED\n";
echo "     in scripts/check-no-committed-secrets.php.\n";
echo "  3. Read the real value from the environment instead.\n";

exit(1);
