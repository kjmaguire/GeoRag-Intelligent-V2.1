<?php

declare(strict_types=1);

namespace Database\Seeders;

use Illuminate\Database\Seeder;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Str;

/**
 * Mechanical geological_ontology seeder — doc-phase 112.
 *
 * Per master-plan §9.3 the ontology population is SME work. This
 * seeder lands the **factual-taxonomy subset** — the three ontology
 * classes whose entries are reference data rather than geological
 * SME judgment:
 *
 *   - commodity        — periodic-table-style element + commodity list
 *   - resource_class   — CIM standard categories
 *   - geological_age   — Archean → Cenozoic + epochs
 *
 * The remaining 9 classes (deposit_model, lithology, alteration,
 * structure, mineral_assemblage, host_rock, tectonic_setting,
 * geochemistry, geophysics) still wait for the §9.3 SME pass — those
 * entries carry geological judgment that this seeder deliberately
 * doesn't presume.
 *
 * Idempotent: uses ON CONFLICT (class, canonical_term) DO NOTHING.
 * Re-running adds nothing on a clean seed but is safe to re-run after
 * SME additions.
 */
class GeologicalOntologyMechanicalSeeder extends Seeder
{
    public function run(): void
    {
        $this->seedCommodities();
        $this->seedResourceClasses();
        $this->seedGeologicalAges();
    }

    /**
     * Periodic-table-grade commodity list. Synonyms include element
     * symbols + common names.
     */
    private function seedCommodities(): void
    {
        $commodities = [
            // Primary mining commodities
            ['canonical' => 'Uranium', 'symbol' => 'U', 'synonyms' => ['U', 'U3O8', 'yellowcake']],
            ['canonical' => 'Gold', 'symbol' => 'Au', 'synonyms' => ['Au']],
            ['canonical' => 'Silver', 'symbol' => 'Ag', 'synonyms' => ['Ag']],
            ['canonical' => 'Copper', 'symbol' => 'Cu', 'synonyms' => ['Cu']],
            ['canonical' => 'Nickel', 'symbol' => 'Ni', 'synonyms' => ['Ni']],
            ['canonical' => 'Cobalt', 'symbol' => 'Co', 'synonyms' => ['Co']],
            ['canonical' => 'Lithium', 'symbol' => 'Li', 'synonyms' => ['Li', 'Li2O', 'lithia']],
            ['canonical' => 'Zinc', 'symbol' => 'Zn', 'synonyms' => ['Zn']],
            ['canonical' => 'Lead', 'symbol' => 'Pb', 'synonyms' => ['Pb']],
            ['canonical' => 'Molybdenum', 'symbol' => 'Mo', 'synonyms' => ['Mo']],
            ['canonical' => 'Tin', 'symbol' => 'Sn', 'synonyms' => ['Sn']],
            ['canonical' => 'Tungsten', 'symbol' => 'W', 'synonyms' => ['W', 'wolfram']],
            ['canonical' => 'Iron', 'symbol' => 'Fe', 'synonyms' => ['Fe']],
            ['canonical' => 'Manganese', 'symbol' => 'Mn', 'synonyms' => ['Mn']],
            ['canonical' => 'Chromium', 'symbol' => 'Cr', 'synonyms' => ['Cr']],
            ['canonical' => 'Vanadium', 'symbol' => 'V', 'synonyms' => ['V']],
            ['canonical' => 'Titanium', 'symbol' => 'Ti', 'synonyms' => ['Ti']],
            ['canonical' => 'Aluminum', 'symbol' => 'Al', 'synonyms' => ['Al', 'aluminium', 'bauxite']],
            // Critical minerals
            ['canonical' => 'Rare Earth Elements', 'symbol' => 'REE', 'synonyms' => ['REE', 'REO', 'rare earths']],
            ['canonical' => 'Antimony', 'symbol' => 'Sb', 'synonyms' => ['Sb']],
            ['canonical' => 'Arsenic', 'symbol' => 'As', 'synonyms' => ['As']],
            ['canonical' => 'Bismuth', 'symbol' => 'Bi', 'synonyms' => ['Bi']],
            ['canonical' => 'Beryllium', 'symbol' => 'Be', 'synonyms' => ['Be', 'beryl']],
            ['canonical' => 'Cadmium', 'symbol' => 'Cd', 'synonyms' => ['Cd']],
            ['canonical' => 'Indium', 'symbol' => 'In', 'synonyms' => ['In']],
            ['canonical' => 'Gallium', 'symbol' => 'Ga', 'synonyms' => ['Ga']],
            ['canonical' => 'Germanium', 'symbol' => 'Ge', 'synonyms' => ['Ge']],
            ['canonical' => 'Tellurium', 'symbol' => 'Te', 'synonyms' => ['Te']],
            ['canonical' => 'Scandium', 'symbol' => 'Sc', 'synonyms' => ['Sc']],
            ['canonical' => 'Niobium', 'symbol' => 'Nb', 'synonyms' => ['Nb', 'columbium']],
            ['canonical' => 'Tantalum', 'symbol' => 'Ta', 'synonyms' => ['Ta']],
            ['canonical' => 'Zirconium', 'symbol' => 'Zr', 'synonyms' => ['Zr']],
            ['canonical' => 'Hafnium', 'symbol' => 'Hf', 'synonyms' => ['Hf']],
            // Platinum-group elements
            ['canonical' => 'Platinum', 'symbol' => 'Pt', 'synonyms' => ['Pt', 'PGM', 'PGE']],
            ['canonical' => 'Palladium', 'symbol' => 'Pd', 'synonyms' => ['Pd', 'PGM', 'PGE']],
            ['canonical' => 'Rhodium', 'symbol' => 'Rh', 'synonyms' => ['Rh', 'PGM', 'PGE']],
            ['canonical' => 'Ruthenium', 'symbol' => 'Ru', 'synonyms' => ['Ru', 'PGM', 'PGE']],
            ['canonical' => 'Iridium', 'symbol' => 'Ir', 'synonyms' => ['Ir', 'PGM', 'PGE']],
            ['canonical' => 'Osmium', 'symbol' => 'Os', 'synonyms' => ['Os', 'PGM', 'PGE']],
            // Non-metallic + industrial
            ['canonical' => 'Diamond', 'symbol' => null, 'synonyms' => ['diamond', 'kimberlite']],
            ['canonical' => 'Potash', 'symbol' => 'K', 'synonyms' => ['K', 'K2O', 'sylvite']],
            ['canonical' => 'Phosphate', 'symbol' => 'P', 'synonyms' => ['P', 'P2O5', 'phosphorite']],
            ['canonical' => 'Sulfur', 'symbol' => 'S', 'synonyms' => ['S']],
            ['canonical' => 'Graphite', 'symbol' => 'C', 'synonyms' => ['C', 'carbon']],
            // Energy
            ['canonical' => 'Oil', 'symbol' => null, 'synonyms' => ['oil', 'petroleum', 'crude']],
            ['canonical' => 'Natural Gas', 'symbol' => null, 'synonyms' => ['gas', 'CH4', 'methane']],
            ['canonical' => 'Coal', 'symbol' => null, 'synonyms' => ['coal', 'thermal coal', 'metallurgical coal']],
        ];

        foreach ($commodities as $c) {
            $payload = ['element_symbol' => $c['symbol']];
            $this->upsertTermWithSynonyms('commodity', $c['canonical'], $c['synonyms'], $payload);
        }
    }

    /**
     * CIM standard resource + reserve categories.
     */
    private function seedResourceClasses(): void
    {
        $classes = [
            ['canonical' => 'Inferred Mineral Resource', 'synonyms' => ['inferred', 'inferred resource']],
            ['canonical' => 'Indicated Mineral Resource', 'synonyms' => ['indicated', 'indicated resource']],
            ['canonical' => 'Measured Mineral Resource', 'synonyms' => ['measured', 'measured resource']],
            ['canonical' => 'Probable Mineral Reserve', 'synonyms' => ['probable', 'probable reserve']],
            ['canonical' => 'Proven Mineral Reserve', 'synonyms' => ['proven', 'proved', 'proven reserve']],
            ['canonical' => 'Exploration Target', 'synonyms' => ['exploration target', 'exploration estimate']],
            ['canonical' => 'Historical Estimate', 'synonyms' => ['historical', 'historical resource']],
        ];

        foreach ($classes as $c) {
            $this->upsertTermWithSynonyms('resource_class', $c['canonical'], $c['synonyms'], ['source' => 'CIM']);
        }
    }

    /**
     * Geological age — eons, eras, periods, epochs.
     */
    private function seedGeologicalAges(): void
    {
        $ages = [
            // Eons
            ['canonical' => 'Hadean Eon', 'kind' => 'eon', 'synonyms' => ['Hadean']],
            ['canonical' => 'Archean Eon', 'kind' => 'eon', 'synonyms' => ['Archean', 'Archaean']],
            ['canonical' => 'Proterozoic Eon', 'kind' => 'eon', 'synonyms' => ['Proterozoic']],
            ['canonical' => 'Phanerozoic Eon', 'kind' => 'eon', 'synonyms' => ['Phanerozoic']],
            // Eras (Proterozoic)
            ['canonical' => 'Paleoproterozoic Era', 'kind' => 'era', 'synonyms' => ['Paleoproterozoic']],
            ['canonical' => 'Mesoproterozoic Era', 'kind' => 'era', 'synonyms' => ['Mesoproterozoic']],
            ['canonical' => 'Neoproterozoic Era', 'kind' => 'era', 'synonyms' => ['Neoproterozoic']],
            // Eras (Phanerozoic)
            ['canonical' => 'Paleozoic Era', 'kind' => 'era', 'synonyms' => ['Paleozoic', 'Palaeozoic']],
            ['canonical' => 'Mesozoic Era', 'kind' => 'era', 'synonyms' => ['Mesozoic']],
            ['canonical' => 'Cenozoic Era', 'kind' => 'era', 'synonyms' => ['Cenozoic', 'Cainozoic']],
            // Paleozoic periods
            ['canonical' => 'Cambrian Period', 'kind' => 'period', 'synonyms' => ['Cambrian']],
            ['canonical' => 'Ordovician Period', 'kind' => 'period', 'synonyms' => ['Ordovician']],
            ['canonical' => 'Silurian Period', 'kind' => 'period', 'synonyms' => ['Silurian']],
            ['canonical' => 'Devonian Period', 'kind' => 'period', 'synonyms' => ['Devonian']],
            ['canonical' => 'Carboniferous Period', 'kind' => 'period', 'synonyms' => ['Carboniferous', 'Mississippian', 'Pennsylvanian']],
            ['canonical' => 'Permian Period', 'kind' => 'period', 'synonyms' => ['Permian']],
            // Mesozoic periods
            ['canonical' => 'Triassic Period', 'kind' => 'period', 'synonyms' => ['Triassic']],
            ['canonical' => 'Jurassic Period', 'kind' => 'period', 'synonyms' => ['Jurassic']],
            ['canonical' => 'Cretaceous Period', 'kind' => 'period', 'synonyms' => ['Cretaceous']],
            // Cenozoic periods + epochs
            ['canonical' => 'Paleogene Period', 'kind' => 'period', 'synonyms' => ['Paleogene', 'Tertiary']],
            ['canonical' => 'Neogene Period', 'kind' => 'period', 'synonyms' => ['Neogene']],
            ['canonical' => 'Quaternary Period', 'kind' => 'period', 'synonyms' => ['Quaternary']],
            ['canonical' => 'Paleocene Epoch', 'kind' => 'epoch', 'synonyms' => ['Paleocene']],
            ['canonical' => 'Eocene Epoch', 'kind' => 'epoch', 'synonyms' => ['Eocene']],
            ['canonical' => 'Oligocene Epoch', 'kind' => 'epoch', 'synonyms' => ['Oligocene']],
            ['canonical' => 'Miocene Epoch', 'kind' => 'epoch', 'synonyms' => ['Miocene']],
            ['canonical' => 'Pliocene Epoch', 'kind' => 'epoch', 'synonyms' => ['Pliocene']],
            ['canonical' => 'Pleistocene Epoch', 'kind' => 'epoch', 'synonyms' => ['Pleistocene', 'ice age']],
            ['canonical' => 'Holocene Epoch', 'kind' => 'epoch', 'synonyms' => ['Holocene', 'Recent']],
        ];

        foreach ($ages as $a) {
            $this->upsertTermWithSynonyms('geological_age', $a['canonical'], $a['synonyms'], ['kind' => $a['kind']]);
        }
    }

    /**
     * Upsert one term + its synonyms. Uses ON CONFLICT DO NOTHING so
     * re-runs are safe.
     *
     * @param array<int, string> $synonyms
     * @param array<string, mixed> $payload
     */
    private function upsertTermWithSynonyms(
        string $class,
        string $canonical,
        array $synonyms,
        array $payload = [],
    ): void {
        // Find-or-create term.
        $existing = DB::table('silver.geological_ontology_terms')
            ->where('class', $class)
            ->where('canonical_term', $canonical)
            ->value('term_id');

        if ($existing) {
            $termId = $existing;
        } else {
            $termId = (string) Str::uuid();
            DB::table('silver.geological_ontology_terms')->insert([
                'term_id' => $termId,
                'class' => $class,
                'canonical_term' => $canonical,
                'payload' => json_encode($payload),
            ]);
        }

        // Insert synonyms; ignore conflicts on the (term_id, synonym,
        // language_code) unique index.
        foreach (array_unique($synonyms) as $syn) {
            DB::table('silver.geological_ontology_synonyms')
                ->insertOrIgnore([
                    'synonym_id' => (string) Str::uuid(),
                    'term_id' => $termId,
                    'synonym' => $syn,
                    'language_code' => 'en',
                    'source' => 'doc-phase-112-mechanical-seed',
                ]);
        }
    }
}
