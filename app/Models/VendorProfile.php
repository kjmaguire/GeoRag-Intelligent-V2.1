<?php

declare(strict_types=1);

namespace App\Models;

use Database\Factories\VendorProfileFactory;
use Illuminate\Database\Eloquent\Factories\HasFactory;
use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\BelongsTo;
use Illuminate\Database\Eloquent\Relations\HasMany;

class VendorProfile extends Model
{
    /** @use HasFactory<VendorProfileFactory> */
    use HasFactory;

    protected $fillable = [
        'name',
        'description',
        'profile_type',
        'is_global',
        'created_by_user_id',
    ];

    protected $casts = [
        'is_global'  => 'boolean',
        'created_at' => 'datetime',
        'updated_at' => 'datetime',
    ];

    /**
     * Allowed values for the profile_type column (mirrors the DB enum).
     */
    public const PROFILE_TYPES = ['lab', 'driller', 'geophysics', 'internal', 'other'];

    /**
     * The user who created this profile.
     */
    public function createdBy(): BelongsTo
    {
        return $this->belongsTo(User::class, 'created_by_user_id');
    }

    /**
     * All column mappings that belong to this vendor profile.
     */
    public function columnMappings(): HasMany
    {
        return $this->hasMany(ColumnMapping::class);
    }
}
