# GeoRAG — Claude Code Project Context

This file is loaded into every Claude Code session in this project. It defines
the rules and conventions that apply to ALL work, regardless of which agent is
active. Keep this file short and high-signal — it gets read every turn.

## What this project is

GeoRAG is a geological intelligence platform that ingests decades of fragmented
exploration data (drill logs, NI 43-101 reports, geophysics, GIS layers) and
lets geologists query it in natural language with cited answers, interactive
visualizations, and export to industry modeling tools. It targets junior mining
and exploration companies with private cloud or on-premise deployment.

## The source of truth

**`georag-architecture.html`** is the complete architecture reference. It
contains every technology decision, data schema, interface contract, deployment
detail, performance tuning, and acceptance criterion. **Read the relevant
section before starting any task.** When code and the architecture doc
disagree, the doc is correct and the code needs fixing (or the doc needs an
explicit, deliberate update with a reason).

New to this project? Start with Section 00 (README) inside the architecture
doc for the reading order.

## Hard rules — never violate

1. **No Streamlit.** Streamlit is permanently rejected. The frontend is React +
   Inertia.js + shadcn/ui + Tailwind. If you see Streamlit referenced anywhere
   in external examples, translate it to our stack.

2. **Async-native drivers only in FastAPI.** `asyncpg` for PostgreSQL,
   `redis.asyncio` (aioredis) for Redis, async Qdrant client, async Neo4j
   driver. Synchronous drivers in async handlers are a blocker-level bug.

3. **Octane-safe Laravel code.** The Laravel app boots once and stays in
   memory. No static state leaks between requests. No singletons holding
   request data.

4. **Citations are mandatory on every RAG response.** Every claim the LLM
   makes must include a `source_chunk_id` or be rejected by Pydantic AI's
   typed output validation. There is no "best-effort" citation mode.

5. **Follow Section 04i hallucination prevention.** All six layers apply to
   any code touching the RAG pipeline. Retrieval quality gate → typed output
   validation → numerical claim verification → entity resolution → chunk
   provenance → geological constraint rules.

6. **Schemas in Section 04e are contracts.** Don't invent fields. Don't skip
   constraints. Don't change enumeration values without SME approval.

7. **Don't duplicate orchestration.** Laravel queues handle user-triggered
   async work. Dagster handles scheduled/bulk data pipelines. Never overlap.

8. **MapLibre GL, not Mapbox GL.** Licensing matters for on-prem deployments.

9. **Neo4j Community Edition only.** No Enterprise features (clustering,
   active page cache warmup, database-level RBAC). Use manual warmup scripts
   and application-level permissions instead.

## Technology snapshot

- **Frontend**: React + Inertia.js, shadcn/ui + Tailwind, MapLibre GL, React Flow, Plotly
- **Application**: Laravel 13 on Octane (Swoole/RoadRunner), Horizon, Reverb, Sanctum, Pulse
- **Domain Service**: FastAPI 0.135.x on Python 3.13, Pydantic AI, asyncpg, aioredis
- **Data Stores**: PostgreSQL 18.3 + PostGIS 3.6.3 (with PgBouncer edoburu 1.25), Neo4j Community 2026.03, Qdrant v1.17, Redis 8.6, SeaweedFS (S3-compatible, replaces MinIO per ADR-0001)
- **Ingestion**: Dagster, Polars, DuckDB, GDAL/GeoPandas, lasio/segyio/obspy, in-process PDF stack (§04p — replaces RAGFlow per ADR-0002)
- **LLM**: vLLM + `Qwen/Qwen3-14B-AWQ` (dev + prod — Ollama cutover complete; legacy Ollama Modelfiles archived under `docker/_deprecated/ollama/`). The earlier Qwen3-30B-A3B MoE was reverted to the 14B dense AWQ in 2026-05 to free VRAM for hatchet-worker-ai (bge-small / bge-reranker-base / SPLADE++) co-tenanting on the dev A4500. Anthropic Claude is wired as optional fallback.

## Agent delegation

This project has specialized Claude Code subagents in `.claude/agents/`. Use
them for focused work — each has its own context window and domain expertise:

- **`senior-reviewer`** (Opus) — architectural review at milestone gates ONLY. Read-only. Sparingly.
- **`backend-laravel`** (Sonnet) — all Laravel work
- **`backend-fastapi`** (Sonnet) — all FastAPI + Pydantic AI work
- **`data-engineer`** (Sonnet) — ingestion pipeline, PostGIS schemas, format parsers
- **`graph-engineer`** (Sonnet) — Neo4j + Cypher
- **`frontend-engineer`** (Sonnet) — React + Inertia + shadcn/ui + visualizations
- **`devops-engineer`** (Sonnet) — Docker Compose, deployment, database tuning
- **`test-engineer`** (Sonnet) — all test writing, golden query sets, snapshot tests
- **`boilerplate-writer`** (Haiku) — migrations, scaffolding, docstrings, simple docs

Claude Code will auto-delegate based on agent descriptions. You can also
invoke explicitly with `@agent-name` in a prompt.

## Budget discipline

Opus is rate-limited on Max plans and shared with other work. Use it only for:
- Initial architecture decisions
- Milestone gate reviews (via `senior-reviewer`)
- Hallucination prevention design discussions
- Interface contract authoring

Everything else goes to Sonnet agents or Haiku for boilerplate.

## Code style

- **Python**: Ruff for linting, Black formatting, type hints everywhere. Pydantic for data models. Use `async def` for anything touching I/O.
- **PHP**: Laravel Pint for formatting. PSR-12 style. Type declarations on all function signatures.
- **TypeScript/React**: Prettier formatting, ESLint. Functional components with hooks. No class components.
- **SQL**: Uppercase keywords, lowercase identifiers, explicit column lists (no `SELECT *` in production code).
- **Cypher**: Parameterized queries always. Lowercase node variable names (`p`, `h`, `f`).

## Commit convention

Conventional commits:
- `feat:` new feature
- `fix:` bug fix
- `refactor:` no behavior change
- `docs:` documentation only
- `test:` test only
- `chore:` tooling, deps, config

Reference the architecture doc section when the commit relates to a specific
part of the spec: `feat(ingestion): implement CRS detection per Section 04b`

## Testing requirements

Every PR should have tests. Golden query tests and hallucination failure tests
are milestone gates — they must pass before a milestone is accepted. See
`test-engineer` agent for patterns.

## When you're stuck

- **Architecture unclear?** Re-read the relevant section in `georag-architecture.html`.
- **Still unclear?** Ask the user (Kyle, the SME). Do not infer geological decisions.
- **Cross-cutting concern?** Invoke `senior-reviewer` for a checkpoint review.
- **Not sure which agent?** Start in the main session and let Claude Code delegate.
- **Operator-style task?** (secret rotation, PII decryption, APP_KEY
  rotation, test-env gotchas) — check `docs/RUNBOOK.md` first. It's the
  single source of truth for procedures that touch encrypted data,
  shared secrets, or reversible ops state.

===

<laravel-boost-guidelines>
=== foundation rules ===

# Laravel Boost Guidelines

The Laravel Boost guidelines are specifically curated by Laravel maintainers for this application. These guidelines should be followed closely to ensure the best experience when building Laravel applications.

## Foundational Context

This application is a Laravel application and its main Laravel ecosystems package & versions are below. You are an expert with them all. Ensure you abide by these specific packages & versions.

- php - 8.5
- inertiajs/inertia-laravel (INERTIA_LARAVEL) - v3
- laravel/ai (AI) - v0
- laravel/framework (LARAVEL) - v13
- laravel/horizon (HORIZON) - v5
- laravel/octane (OCTANE) - v2
- laravel/prompts (PROMPTS) - v0
- laravel/pulse (PULSE) - v1
- laravel/reverb (REVERB) - v1
- laravel/sanctum (SANCTUM) - v4
- livewire/livewire (LIVEWIRE) - v4
- laravel/boost (BOOST) - v2
- laravel/mcp (MCP) - v0
- laravel/pail (PAIL) - v1
- laravel/pint (PINT) - v1
- phpunit/phpunit (PHPUNIT) - v12
- @inertiajs/react (INERTIA_REACT) - v2
- laravel-echo (ECHO) - v2
- react (REACT) - v19
- tailwindcss (TAILWINDCSS) - v4

## Skills Activation

This project has domain-specific skills available in `**/skills/**`. You MUST activate the relevant skill whenever you work in that domain—don't wait until you're stuck.

## Conventions

- You must follow all existing code conventions used in this application. When creating or editing a file, check sibling files for the correct structure, approach, and naming.
- Use descriptive names for variables and methods. For example, `isRegisteredForDiscounts`, not `discount()`.
- Check for existing components to reuse before writing a new one.

## Verification Scripts

- Do not create verification scripts or tinker when tests cover that functionality and prove they work. Unit and feature tests are more important.

## Application Structure & Architecture

- Stick to existing directory structure; don't create new base folders without approval.
- Do not change the application's dependencies without approval.

## Frontend Bundling

- If the user doesn't see a frontend change reflected in the UI, it could mean they need to run `npm run build`, `npm run dev`, or `composer run dev`. Ask them.

## Documentation Files

- You must only create documentation files if explicitly requested by the user.

## Replies

- Be concise in your explanations - focus on what's important rather than explaining obvious details.

=== boost rules ===

# Laravel Boost

## Tools

- Laravel Boost is an MCP server with tools designed specifically for this application. Prefer Boost tools over manual alternatives like shell commands or file reads.
- Use `database-query` to run read-only queries against the database instead of writing raw SQL in tinker.
- Use `database-schema` to inspect table structure before writing migrations or models.
- Use `get-absolute-url` to resolve the correct scheme, domain, and port for project URLs. Always use this before sharing a URL with the user.
- Use `browser-logs` to read browser logs, errors, and exceptions. Only recent logs are useful, ignore old entries.

## Searching Documentation (IMPORTANT)

- Always use `search-docs` before making code changes. Do not skip this step. It returns version-specific docs based on installed packages automatically.
- Pass a `packages` array to scope results when you know which packages are relevant.
- Use multiple broad, topic-based queries: `['rate limiting', 'routing rate limiting', 'routing']`. Expect the most relevant results first.
- Do not add package names to queries because package info is already shared. Use `test resource table`, not `filament 4 test resource table`.

### Search Syntax

1. Use words for auto-stemmed AND logic: `rate limit` matches both "rate" AND "limit".
2. Use `"quoted phrases"` for exact position matching: `"infinite scroll"` requires adjacent words in order.
3. Combine words and phrases for mixed queries: `middleware "rate limit"`.
4. Use multiple queries for OR logic: `queries=["authentication", "middleware"]`.

## Artisan

- Run Artisan commands directly via the command line (e.g., `php artisan route:list`). Use `php artisan list` to discover available commands and `php artisan [command] --help` to check parameters.
- Inspect routes with `php artisan route:list`. Filter with: `--method=GET`, `--name=users`, `--path=api`, `--except-vendor`, `--only-vendor`.
- Read configuration values using dot notation: `php artisan config:show app.name`, `php artisan config:show database.default`. Or read config files directly from the `config/` directory.
- To check environment variables, read the `.env` file directly.

## Tinker

- Execute PHP in app context for debugging and testing code. Do not create models without user approval, prefer tests with factories instead. Prefer existing Artisan commands over custom tinker code.
- Always use single quotes to prevent shell expansion: `php artisan tinker --execute 'Your::code();'`
  - Double quotes for PHP strings inside: `php artisan tinker --execute 'User::where("active", true)->count();'`

=== php rules ===

# PHP

- Always use curly braces for control structures, even for single-line bodies.
- Use PHP 8 constructor property promotion: `public function __construct(public GitHub $github) { }`. Do not leave empty zero-parameter `__construct()` methods unless the constructor is private.
- Use explicit return type declarations and type hints for all method parameters: `function isAccessible(User $user, ?string $path = null): bool`
- Use TitleCase for Enum keys: `FavoritePerson`, `BestLake`, `Monthly`.
- Prefer PHPDoc blocks over inline comments. Only add inline comments for exceptionally complex logic.
- Use array shape type definitions in PHPDoc blocks.

=== deployments rules ===

# Deployment

- Laravel can be deployed using [Laravel Cloud](https://cloud.laravel.com/), which is the fastest way to deploy and scale production Laravel applications.

=== tests rules ===

# Test Enforcement

- Every change must be programmatically tested. Write a new test or update an existing test, then run the affected tests to make sure they pass.
- Run the minimum number of tests needed to ensure code quality and speed. Use `php artisan test --compact` with a specific filename or filter.

=== inertia-laravel/core rules ===

# Inertia

- Inertia creates fully client-side rendered SPAs without modern SPA complexity, leveraging existing server-side patterns.
- Components live in `resources/js/Pages` (unless specified in `vite.config.js`). Use `Inertia::render()` for server-side routing instead of Blade views.
- ALWAYS use `search-docs` tool for version-specific Inertia documentation and updated code examples.
- IMPORTANT: Activate `inertia-react-development` when working with Inertia client-side patterns.

# Inertia v3

- Use all Inertia features from v1, v2, and v3. Check the documentation before making changes to ensure the correct approach.
- New v3 features: standalone HTTP requests (`useHttp` hook), optimistic updates with automatic rollback, layout props (`useLayoutProps` hook), instant visits, simplified SSR via `@inertiajs/vite` plugin, custom exception handling for error pages.
- Carried over from v2: deferred props, infinite scroll, merging props, polling, prefetching, once props, flash data.
- When using deferred props, add an empty state with a pulsing or animated skeleton.
- Axios has been removed. Use the built-in XHR client with interceptors, or install Axios separately if needed.
- `Inertia::lazy()` / `LazyProp` has been removed. Use `Inertia::optional()` instead.
- Prop types (`Inertia::optional()`, `Inertia::defer()`, `Inertia::merge()`) work inside nested arrays with dot-notation paths.
- SSR works automatically in Vite dev mode with `@inertiajs/vite` - no separate Node.js server needed during development.
- Event renames: `invalid` is now `httpException`, `exception` is now `networkError`.
- `router.cancel()` replaced by `router.cancelAll()`.
- The `future` configuration namespace has been removed - all v2 future options are now always enabled.

=== laravel/core rules ===

# Do Things the Laravel Way

- Use `php artisan make:` commands to create new files (i.e. migrations, controllers, models, etc.). You can list available Artisan commands using `php artisan list` and check their parameters with `php artisan [command] --help`.
- If you're creating a generic PHP class, use `php artisan make:class`.
- Pass `--no-interaction` to all Artisan commands to ensure they work without user input. You should also pass the correct `--options` to ensure correct behavior.

### Model Creation

- When creating new models, create useful factories and seeders for them too. Ask the user if they need any other things, using `php artisan make:model --help` to check the available options.

## APIs & Eloquent Resources

- For APIs, default to using Eloquent API Resources and API versioning unless existing API routes do not, then you should follow existing application convention.

## URL Generation

- When generating links to other pages, prefer named routes and the `route()` function.

## Testing

- When creating models for tests, use the factories for the models. Check if the factory has custom states that can be used before manually setting up the model.
- Faker: Use methods such as `$this->faker->word()` or `fake()->randomDigit()`. Follow existing conventions whether to use `$this->faker` or `fake()`.
- When creating tests, make use of `php artisan make:test [options] {name}` to create a feature test, and pass `--unit` to create a unit test. Most tests should be feature tests.

## Vite Error

- If you receive an "Illuminate\Foundation\ViteException: Unable to locate file in Vite manifest" error, you can run `npm run build` or ask the user to run `npm run dev` or `composer run dev`.

=== octane/core rules ===

# Octane

- Octane boots the application once and reuses it across requests, so singletons persist between requests.
- The Laravel container's `scoped` method may be used as a safe alternative to `singleton`.
- Never inject the container, request, or config repository into a singleton's constructor; use a resolver closure or `bind()` instead:

```php
// Bad
$this->app->singleton(Service::class, fn (Application $app) => new Service($app['request']));

// Good
$this->app->singleton(Service::class, fn () => new Service(fn () => request()));
```

- Never append to static properties, as they accumulate in memory across requests.

=== pint/core rules ===

# Laravel Pint Code Formatter

- If you have modified any PHP files, you must run `vendor/bin/pint --dirty --format agent` before finalizing changes to ensure your code matches the project's expected style.
- Do not run `vendor/bin/pint --test --format agent`, simply run `vendor/bin/pint --format agent` to fix any formatting issues.

=== phpunit/core rules ===

# PHPUnit

- This application uses PHPUnit for testing. All tests must be written as PHPUnit classes. Use `php artisan make:test --phpunit {name}` to create a new test.
- If you see a test using "Pest", convert it to PHPUnit.
- Every time a test has been updated, run that singular test.
- When the tests relating to your feature are passing, ask the user if they would like to also run the entire test suite to make sure everything is still passing.
- Tests should cover all happy paths, failure paths, and edge cases.
- You must not remove any tests or test files from the tests directory without approval. These are not temporary or helper files; these are core to the application.

## Running Tests

- Run the minimal number of tests, using an appropriate filter, before finalizing.
- To run all tests: `php artisan test --compact`.
- To run all tests in a file: `php artisan test --compact tests/Feature/ExampleTest.php`.
- To filter on a particular test name: `php artisan test --compact --filter=testName` (recommended after making a change to a related file).

=== inertia-react/core rules ===

# Inertia + React

- IMPORTANT: Activate `inertia-react-development` when working with Inertia React client-side patterns.

</laravel-boost-guidelines>
