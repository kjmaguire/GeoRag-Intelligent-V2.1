<?php

declare(strict_types=1);

namespace App\Models;

use Database\Factories\ColumnMappingFactory;
use Illuminate\Database\Eloquent\Factories\HasFactory;
use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\BelongsTo;

class ColumnMapping extends Model
{
    /** @use HasFactory<ColumnMappingFactory> */
    use HasFactory;

    protected $fillable = [
        'vendor_profile_id',
        'parser_type',
        'canonical_field',
        'source_column',
        'source_unit',
        'target_unit',
        'notes',
    ];

    protected $casts = [
        'created_at' => 'datetime',
        'updated_at' => 'datetime',
    ];

    /**
     * Allowed values for the parser_type column (mirrors the DB enum).
     */
    public const PARSER_TYPES = [
        'csv_collar',
        'csv_sample',
        'csv_survey',
        'csv_lithology',
        'xlsx',
        'spatial',
        'pdf_report',
        'docx',
        'raster',
    ];

    /**
     * The vendor profile this mapping belongs to.
     */
    public function vendorProfile(): BelongsTo
    {
        return $this->belongsTo(VendorProfile::class);
    }
}
