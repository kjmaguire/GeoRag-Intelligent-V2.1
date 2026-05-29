<?php

declare(strict_types=1);

namespace Database\Seeders;

use App\Models\User;
use Database\Seeders\PublicGeoscience\CanadaJurisdictionsSeeder;
use Database\Seeders\PublicGeoscience\CommodityAliasesSeeder;
use Database\Seeders\PublicGeoscience\StatusAliasesSeeder;
use Illuminate\Database\Console\Seeds\WithoutModelEvents;
use Illuminate\Database\Seeder;

class DatabaseSeeder extends Seeder
{
    use WithoutModelEvents;

    /**
     * Seed the application's database.
     */
    public function run(): void
    {
        // User::factory(10)->create();

        User::factory()->create([
            'name' => 'Test User',
            'email' => 'test@example.com',
        ]);

        $this->call([
            // Public Geoscience — order matters. Jurisdictions first, then
            // crosswalks (status_aliases FK-references jurisdictions).
            CanadaJurisdictionsSeeder::class,
            CommodityAliasesSeeder::class,
            StatusAliasesSeeder::class,

            // Doc-phase 112 — mechanical geological_ontology reference
            // data (commodity + geological_age + resource_class).
            // Idempotent. The other 9 §20.1 classes wait for the §9.3
            // SME pass.
            GeologicalOntologyMechanicalSeeder::class,
        ]);
    }
}
