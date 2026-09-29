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
 * credential-shaped assignments (PASSWORD/SECRET/TOKEN/…_KEY names), DSN
 * credentials and Laravel `base64:` keys, and it only complains about values
 * that look random. Placeholders are what belongs in the repo, so they are
 * allowed by name or by an obvious marker word rather than by entropy.
 *
 * SEC-6 (2026-09-29): a real APP_KEY and FASTAPI_SERVICE_KEY sat in
 * ops/audit/2026-04-19-resolved-compose-all-profiles.yml while this reported
 * clean. Two blind spots: `*_KEY` names were never inspected, and any value
 * containing `-` or `_` was waved through as a placeholder — which is every
 * `secrets.token_urlsafe()` value ever generated. Both are closed below, and a
 * hit is now printed as a fingerprint rather than the value, so CI logs stop
 * being a second copy of whatever it finds.
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

/**
 * Words that only appear in a value somebody wrote to be replaced. Checked
 * case-insensitively, on the value and — for `base64:` keys — on what it
 * decodes to (phpunit.pgsql.xml's key decodes to
 * "phpunit-testing-key-not-a-secret").
 */
const PLACEHOLDER_MARKERS = [
    'changeme', 'change-me', 'change_me', 'placeholder', 'example', 'replace',
    'dummy', 'not-a-real', 'not-a-secret', 'not_a_secret', 'notasecret',
    'your-', 'your_', 'xxxx', 'fake', 'sample', 'test', 'dev-only', 'do-not-use',
    'rotate', 'insecure', 'redacted',
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
    if (str_contains($value, '$') || str_contains($value, '{')) {
        return false;  // shell / compose interpolation, not a literal
    }

    // An unsigned JWT carries no secret by construction — the header says
    // `alg: none` and the whole thing base64-decodes to public claims. The
    // Hatchet dev token in the CI workflows is one of these.
    if (str_starts_with($value, 'eyJhbGciOiAibm9uZSI')) {
        return false;
    }

    // A Laravel APP_KEY. Judge what it decodes to: 32 random bytes are a
    // key; "phpunit-testing-key-not-a-secret" and "aaaa…" are not.
    if (stripos($value, 'base64:') === 0) {
        $decoded = base64_decode(substr($value, 7), true);

        return $decoded !== false
            && strlen($decoded) >= 16
            && ! isPlaceholder($decoded)
            && count(array_unique(str_split($decoded))) >= 8;
    }

    if (isPlaceholder($value) || str_contains($value, ' ') || strlen($value) < 16) {
        return false;
    }

    // Code, not a literal: `settings.ANTHROPIC_MAX_OUTPUT_TOKENS`,
    // `CommodityKey3D[]`, `fn(...)`. `_KEY` names made these reachable.
    if (strpbrk($value, '[]()<>') !== false) {
        return false;
    }
    if (preg_match('/^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+$/', $value) === 1
        && ! str_starts_with($value, 'eyJ')) {
        return false;
    }

    // An object-store key or a path (`minio_key="collars/<uuid>/x.csv"`).
    if (str_contains($value, '/') && preg_match('/\.[A-Za-z0-9]{1,5}$/', $value) === 1) {
        return false;
    }

    $classes = preg_match('/[a-z]/', $value) + preg_match('/[A-Z]/', $value) + preg_match('/[0-9]/', $value);

    // 32+ characters: separators no longer mean "placeholder". This is
    // secrets.token_urlsafe() / openssl rand -hex territory, and those
    // values contain `-` and `_` as a matter of course. What still reads as
    // a name rather than a key: lowercase words joined by separators
    // (including UUIDs), and anything with few distinct characters.
    if (strlen($value) >= 32) {
        if (preg_match('#^[A-Za-z0-9_\-+/=.]+$#', $value) !== 1) {
            return false;
        }
        if (isWords($value)) {
            return false;
        }

        return $classes >= 2 && count(array_unique(str_split($value))) >= 10;
    }

    if (str_contains($value, '_') || str_contains($value, '-')) {
        return false;
    }

    return $classes === 3;
}

/**
 * Words joined by separators — `sidecar-test-key-abc123`,
 * `REVERB_APP_KEY-for-local`, a UUID — rather than a random token.
 * Each segment has to read as one word (a single case, digits, or a word
 * followed by a number); the segments of a random token almost never all do.
 */
function isWords(string $value): bool
{
    $segments = preg_split('/[._-]+/', $value, -1, PREG_SPLIT_NO_EMPTY) ?: [];
    if (count($segments) < 3) {
        return false;
    }

    foreach ($segments as $segment) {
        if (preg_match('/^(?:[a-z]+|[A-Z]+|[A-Z][a-z]+|[0-9]+|[a-z]+[0-9]+|[0-9a-f]{1,12})$/', $segment) !== 1) {
            return false;
        }
    }

    return true;
}

function isPlaceholder(string $value): bool
{
    $lower = strtolower($value);
    foreach (PLACEHOLDER_MARKERS as $marker) {
        if (str_contains($lower, $marker)) {
            return true;
        }
    }

    return false;
}

/**
 * How a hit is shown: enough to find it and to compare it against a value
 * you already hold, not enough to use it.
 */
function fingerprint(string $value): string
{
    return sprintf('<%d chars, sha256:%s>', strlen($value), substr(hash('sha256', $value), 0, 12));
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
    // KEY: value / KEY=value / "KEY" => "value". `_KEY` / `APIKEY` names
    // (APP_KEY, FASTAPI_SERVICE_KEY, COHERE_API_KEY, AWS_SECRET_ACCESS_KEY…)
    // were outside this until SEC-6.
    '/(?:PASSWORD|PASSWD|PWD|SECRET|TOKEN|_KEY|APIKEY)[A-Z0-9_]*\s*[:=>]+\s*[\'"]?([^\'"\s,;)]+)/i',
    // A Laravel encryption key, whatever it is assigned to.
    '#\b(base64:[A-Za-z0-9+/]{40,}={0,2})#',
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
    '/\$\{[A-Z0-9_]*(?:PASSWORD|PASSWD|PWD|SECRET|TOKEN|_KEY|APIKEY)[A-Z0-9_]*:[-=]([^}\s]+)\}/i',
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
                $hits[] = sprintf('%s:%d  %s', $path, $n + 1, fingerprint($value));
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
