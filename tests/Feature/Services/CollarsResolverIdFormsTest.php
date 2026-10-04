<?php

declare(strict_types=1);

namespace Tests\Feature\Services;

use App\Services\Citations\Resolvers\CollarsResolver;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Str;
use Tests\Concerns\RequiresPostgres;
use Tests\TestCase;

/**
 * FastAPI (response_assembler.py) emits three `silver.collars:` shapes:
 * the spatial `count=N:first=<uuid>`, the single-collar
 * `hole=X:collar=<uuid>:assays=N:litho=N`, and `miss`. The resolver used to
 * parse only `first=`, so the details form fell through to the generic
 * "Collar data query result" card with no collar behind it.
 */
final class CollarsResolverIdFormsTest extends TestCase
{
    use RefreshDatabase;
    use RequiresPostgres;

    private string $workspaceId;

    private string $projectId;

    private string $collarId;

    protected function setUp(): void
    {
        parent::setUp();

        $this->workspaceId = (string) Str::uuid();
        $this->projectId = (string) Str::uuid();
        $this->collarId = (string) Str::uuid();
        DB::statement(
            'INSERT INTO silver.workspaces (workspace_id, name, slug) VALUES (?::uuid, ?, ?)',
            [$this->workspaceId, 'forms-ws', 'forms-'.substr($this->workspaceId, 0, 8)],
        );
        DB::statement(
            'INSERT INTO silver.projects (project_id, project_name, slug, workspace_id) VALUES (?::uuid, ?, ?, ?::uuid)',
            [$this->projectId, 'forms-project', 'forms-'.substr($this->projectId, 0, 8), $this->workspaceId],
        );
        DB::statement(
            "INSERT INTO silver.collars (collar_id, hole_id, project_id, workspace_id, easting, northing,
                    total_depth, hole_type, status)
             VALUES (?::uuid, 'PLS-20-01', ?::uuid, ?::uuid, 1, 2, 150.5, 'DD', 'active')",
            [$this->collarId, $this->projectId, $this->workspaceId],
        );
    }

    public function test_the_first_form_resolves_the_collar(): void
    {
        $response = (new CollarsResolver)->resolve(
            "silver.collars:count=20:first={$this->collarId}",
            $this->workspaceId,
            [$this->projectId],
        );

        $this->assertSame(200, $response->getStatusCode());
        $this->assertSame('Drill Collar: PLS-20-01', $response->getData(true)['title']);
    }

    public function test_the_collar_details_form_resolves_the_collar(): void
    {
        $sourceId = "silver.collars:hole=PLS-20-01:collar={$this->collarId}:assays=12:litho=4";

        $response = (new CollarsResolver)->resolve($sourceId, $this->workspaceId, [$this->projectId]);
        $payload = $response->getData(true);

        $this->assertSame(200, $response->getStatusCode());
        $this->assertSame('Drill Collar: PLS-20-01', $payload['title']);
        $this->assertSame($sourceId, $payload['source_chunk_id']);
        $this->assertSame($this->collarId, $payload['metadata']['collar_id']);
    }

    public function test_the_collar_details_form_is_scoped_to_the_callers_projects(): void
    {
        $response = (new CollarsResolver)->resolve(
            "silver.collars:hole=PLS-20-01:collar={$this->collarId}:assays=1:litho=1",
            $this->workspaceId,
            [(string) Str::uuid()],
        );

        $this->assertSame(404, $response->getStatusCode());
        $this->assertSame('Collar not found', $response->getData(true)['text']);
    }

    public function test_the_collar_details_form_fails_closed_without_scope(): void
    {
        $sourceId = "silver.collars:hole=PLS-20-01:collar={$this->collarId}:assays=1:litho=1";

        $this->assertSame(404, (new CollarsResolver)->resolve($sourceId, null, null)->getStatusCode());
        $this->assertSame(404, (new CollarsResolver)->resolve($sourceId, $this->workspaceId, [])->getStatusCode());
    }

    public function test_the_miss_marker_is_a_404(): void
    {
        $response = (new CollarsResolver)->resolve('silver.collars:miss', $this->workspaceId, [$this->projectId]);

        $this->assertSame(404, $response->getStatusCode());
        $this->assertSame('Collar not found', $response->getData(true)['text']);
    }

    public function test_a_bare_count_keeps_the_generic_summary(): void
    {
        $response = (new CollarsResolver)->resolve('silver.collars:count=0', $this->workspaceId, [$this->projectId]);

        $this->assertSame(200, $response->getStatusCode());
        $this->assertSame('Collar data query result', $response->getData(true)['text']);
    }
}
