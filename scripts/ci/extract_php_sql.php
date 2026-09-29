<?php

declare(strict_types=1);

/**
 * Emit, as JSON, the SQL the PHP application sends to Postgres, for
 * scripts/ci/check_sql_against_schema.py to check against a migrated
 * database (database audit 2026-09-29 PG-15; lifted from the auditor's
 * php_strings.php / php_qb2.php).
 *
 *   strings — SQL string literals ('.'-concatenation folded; interpolated
 *             pieces become __PH__). Only strings that start with
 *             SELECT/INSERT/UPDATE/DELETE/WITH and contain FROM/INTO/SET.
 *   chains  — DB::table('x') query-builder chains: the table (plus join
 *             tables and aliases) and the column names passed to column-
 *             taking methods.
 *   models  — Eloquent `protected $table = '...'` declarations.
 *
 * Token-based, no application boot: it must run in CI before anything is
 * configured. Deliberately conservative — anything it cannot read
 * statically (closures, variables, expressions) is skipped, not guessed.
 *
 * Usage: php scripts/ci/extract_php_sql.php [root ...]   (default: app)
 */
$repo = realpath(__DIR__.'/../..');
$roots = array_slice($argv, 1) ?: ['app'];

/** @return list<string> */
function phpFiles(string $root): array
{
    $out = [];
    $it = new RecursiveIteratorIterator(new RecursiveDirectoryIterator($root, FilesystemIterator::SKIP_DOTS));
    foreach ($it as $f) {
        if ($f->isFile() && str_ends_with($f->getFilename(), '.php')) {
            $out[] = $f->getPathname();
        }
    }
    sort($out);

    return $out;
}

function literal(array $tok): string
{
    $s = $tok[1];
    $inner = substr($s, 1, -1);

    return $s[0] === "'"
        ? str_replace(["\\'", '\\\\'], ["'", '\\'], $inner)
        : stripcslashes($inner);
}

/** @return list<array{line: int, sql: string, interp: bool}> */
function sqlStrings(array $toks): array
{
    $out = [];
    $n = count($toks);
    $i = 0;
    while ($i < $n) {
        $buf = '';
        $line = null;
        $interp = false;
        $any = false;
        while ($i < $n) {
            $t = $toks[$i];
            if (is_array($t) && $t[0] === T_CONSTANT_ENCAPSED_STRING) {
                $buf .= literal($t);
                $line ??= $t[2];
                $any = true;
                $i++;
            } elseif ($t === '"' || (is_array($t) && $t[0] === T_START_HEREDOC)) {
                $line ??= is_array($t) ? $t[2] : null;
                $heredoc = $t !== '"';
                $nowdoc = $heredoc && str_contains($t[1], "'");
                $i++;
                while ($i < $n) {
                    $u = $toks[$i];
                    if (! $heredoc && $u === '"') {
                        $i++;
                        break;
                    }
                    if ($heredoc && is_array($u) && $u[0] === T_END_HEREDOC) {
                        $i++;
                        break;
                    }
                    if (is_array($u) && $u[0] === T_ENCAPSED_AND_WHITESPACE) {
                        $buf .= $nowdoc || $heredoc ? $u[1] : stripcslashes($u[1]);
                        $line ??= $u[2];
                    } elseif (is_array($u) && in_array($u[0], [T_CURLY_OPEN, T_DOLLAR_OPEN_CURLY_BRACES], true)) {
                        $depth = 1;
                        $i++;
                        while ($i < $n && $depth > 0) {
                            $v = $toks[$i];
                            if ($v === '{' || (is_array($v) && in_array($v[0], [T_CURLY_OPEN, T_DOLLAR_OPEN_CURLY_BRACES], true))) {
                                $depth++;
                            }
                            if ($v === '}') {
                                $depth--;
                            }
                            if ($depth > 0) {
                                $i++;
                            }
                        }
                        $buf .= '__PH__';
                        $interp = true;
                    } elseif (is_array($u) && $u[0] === T_VARIABLE) {
                        $buf .= '__PH__';
                        $interp = true;
                    }
                    $i++;
                }
                $any = true;
            } elseif (is_array($t) && $t[0] === T_WHITESPACE && $any) {
                $i++;
            } elseif ($t === '.' && $any) {
                $j = $i + 1;
                while ($j < $n && is_array($toks[$j]) && in_array($toks[$j][0], [T_WHITESPACE, T_COMMENT], true)) {
                    $j++;
                }
                $next = $toks[$j] ?? null;
                if ((is_array($next) && in_array($next[0], [T_CONSTANT_ENCAPSED_STRING, T_START_HEREDOC], true)) || $next === '"') {
                    $i = $j;

                    continue;
                }
                // Concatenated expression: skip it, leave a placeholder.
                $buf .= '__PH__';
                $interp = true;
                $i = $j;
                $depth = 0;
                while ($i < $n) {
                    $v = $toks[$i];
                    if ($v === '(' || $v === '[') {
                        $depth++;
                    }
                    if ($v === ')' || $v === ']') {
                        if ($depth === 0) {
                            break;
                        }
                        $depth--;
                    }
                    if ($depth === 0 && in_array($v, ['.', ',', ';'], true)) {
                        break;
                    }
                    $i++;
                }
            } else {
                break;
            }
        }
        if ($any) {
            if (preg_match('/^\s*(SELECT|INSERT|UPDATE|DELETE|WITH)\b/i', $buf)
                && preg_match('/\b(FROM|INTO|SET)\b/i', $buf)) {
                $out[] = ['line' => (int) $line, 'sql' => $buf, 'interp' => $interp];
            }
        } else {
            $i++;
        }
    }

    return $out;
}

/** @return list<array{line: int, tables: list<string>, refs: list<array{0: string, 1: string, 2: int}>, methods: list<string>}> */
function builderChains(array $all): array
{
    $colMethods = ['where', 'orWhere', 'whereIn', 'whereNotIn', 'whereNull', 'whereNotNull', 'orderBy',
        'orderByDesc', 'groupBy', 'pluck', 'value', 'sum', 'avg', 'max', 'min', 'whereBetween', 'latest',
        'oldest', 'whereDate', 'increment', 'decrement', 'orWhereIn', 'orWhereNull', 'firstWhere', 'count'];
    $selMethods = ['select', 'addSelect', 'get', 'first'];
    $writeMethods = ['insert', 'update', 'upsert', 'updateOrInsert', 'insertOrIgnore', 'insertGetId'];

    $toks = array_values(array_filter($all, fn ($t) => ! (is_array($t) && in_array($t[0], [T_WHITESPACE, T_COMMENT, T_DOC_COMMENT], true))));
    $n = count($toks);
    $out = [];
    for ($i = 0; $i < $n - 4; $i++) {
        $t = $toks[$i];
        if (! (is_array($t) && $t[1] === 'DB'
            && is_array($toks[$i + 1]) && $toks[$i + 1][0] === T_DOUBLE_COLON
            && is_array($toks[$i + 2]) && $toks[$i + 2][1] === 'table'
            && $toks[$i + 3] === '('
            && is_array($toks[$i + 4]) && $toks[$i + 4][0] === T_CONSTANT_ENCAPSED_STRING
            && ($toks[$i + 5] ?? null) === ')')) {
            continue;
        }
        $chain = ['line' => $t[2], 'tables' => [literal($toks[$i + 4])], 'refs' => [], 'methods' => []];
        $j = $i + 6;
        $depth = 0;
        $method = null;
        $stack = [];
        $closureDepth = null;
        while ($j < $n) {
            $u = $toks[$j];
            if ($u === '(' || $u === '[' || $u === '{') {
                $depth++;
                $stack[] = $method;
            } elseif ($u === ')' || $u === ']' || $u === '}') {
                $depth--;
                $method = array_pop($stack);
                if ($closureDepth !== null && $depth < $closureDepth) {
                    $closureDepth = null;
                }
                if ($depth < 0) {
                    break;
                }
            } elseif ($u === ';' && $depth <= 0) {
                break;
            } elseif (is_array($u) && in_array($u[0], [T_FUNCTION, T_FN], true)) {
                // Closure bodies reference other tables/aliases; skip them.
                $closureDepth ??= $depth + 1;
            } elseif ($closureDepth === null && is_array($u) && in_array($u[0], [T_OBJECT_OPERATOR, T_NULLSAFE_OBJECT_OPERATOR], true)
                && is_array($toks[$j + 1] ?? null) && $toks[$j + 1][0] === T_STRING && ($toks[$j + 2] ?? null) === '(') {
                $method = $toks[$j + 1][1];
                $chain['methods'][] = $method;
                $j += 2;
                $depth++;
                $stack[] = $method;
                $v = $toks[$j + 1] ?? null;
                if (in_array($method, ['join', 'leftJoin', 'rightJoin'], true) && is_array($v) && $v[0] === T_CONSTANT_ENCAPSED_STRING) {
                    $chain['tables'][] = literal($v);
                }
                $j++;

                continue;
            } elseif ($closureDepth === null && is_array($u) && $u[0] === T_CONSTANT_ENCAPSED_STRING && $method !== null) {
                $s = literal($u);
                $next = $toks[$j + 1] ?? null;
                $prev = $toks[$j - 1] ?? null;
                $isKey = is_array($next) && $next[0] === T_DOUBLE_ARROW;
                if (in_array($method, $writeMethods, true) && $isKey) {
                    $chain['refs'][] = [$method, $s, $u[2]];
                } elseif (in_array($method, $selMethods, true) && ! $isKey) {
                    $chain['refs'][] = [$method, $s, $u[2]];
                } elseif (in_array($method, $colMethods, true) && $prev === '(') {
                    // First argument only: later ones (whereIn's value list,
                    // where's operand) are values, not columns.
                    $chain['refs'][] = [$method, $s, $u[2]];
                }
            }
            $j++;
        }
        $out[] = $chain;
    }

    return $out;
}

$result = ['strings' => [], 'chains' => [], 'models' => []];
foreach ($roots as $root) {
    foreach (phpFiles($repo.'/'.$root) as $path) {
        $rel = ltrim(substr($path, strlen($repo)), '/');
        $src = (string) file_get_contents($path);
        $toks = token_get_all($src);
        foreach (sqlStrings($toks) as $s) {
            $result['strings'][] = ['file' => $rel] + $s;
        }
        foreach (builderChains($toks) as $c) {
            $result['chains'][] = ['file' => $rel] + $c;
        }
        if (preg_match('/protected\s+\$table\s*=\s*[\'"]([\w.]+)[\'"]/', $src, $m, PREG_OFFSET_CAPTURE)) {
            $line = substr_count(substr($src, 0, $m[1][1]), "\n") + 1;
            $result['models'][] = ['file' => $rel, 'line' => $line, 'table' => $m[1][0]];
        }
    }
}

echo json_encode($result, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES), "\n";
