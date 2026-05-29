<?php

namespace Tests\Feature\Services\Ingestion;

use Tests\TestCase;

class WorkspaceDataVersionBumperTest extends TestCase
{
    /**
     * A basic feature test example.
     */
    public function test_example(): void
    {
        $response = $this->get('/');

        $response->assertStatus(200);
    }
}
