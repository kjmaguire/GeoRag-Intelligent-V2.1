<?php

declare(strict_types=1);

namespace App\Models;

use Illuminate\Database\Eloquent\Casts\Attribute;
use Illuminate\Database\Eloquent\Concerns\HasUuids;
use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\BelongsTo;

class Alteration extends Model
{
    use HasUuids;

    // Migration 2026_05_20_060400 dropped the empty plural `silver.alterations`
    // and replaced it with the singular spec table. New shape: `id` (uuid) PK,
    // `workspace_id` (RLS), `minerals text[]`, plus a `notes` field.
    protected $table = 'silver.alteration';

    protected $primaryKey = 'id';

    public $incrementing = false;

    protected $keyType = 'string';

    public $timestamps = false;

    protected $fillable = [
        'workspace_id',
        'collar_id',
        'from_depth',
        'to_depth',
        'alteration_type',
        'intensity',
        'minerals',
        'notes',
    ];

    protected $casts = [
        'from_depth' => 'float',
        'to_depth' => 'float',
        'created_at' => 'datetime',
    ];

    public function collar(): BelongsTo
    {
        return $this->belongsTo(Collar::class, 'collar_id', 'collar_id');
    }

    // PG text[] surfaces as the literal '{quartz,sericite}' over PDO. Parse on
    // read so the JSON response carries a real array; accept array-or-null on
    // write and let the driver bind it natively.
    protected function minerals(): Attribute
    {
        return Attribute::make(
            get: function ($value) {
                if ($value === null || $value === '') {
                    return [];
                }
                if (is_array($value)) {
                    return $value;
                }
                $inner = trim((string) $value, '{}');
                if ($inner === '') {
                    return [];
                }

                return array_map(
                    static fn (string $m) => trim($m, " \"\t\n\r\0\x0B"),
                    str_getcsv($inner),
                );
            },
        );
    }
}
