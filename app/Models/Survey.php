<?php

declare(strict_types=1);

namespace App\Models;

use App\Enums\SurveyMethod;
use Illuminate\Database\Eloquent\Concerns\HasUuids;
use Illuminate\Database\Eloquent\Factories\HasFactory;
use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\BelongsTo;

class Survey extends Model
{
    use HasFactory;
    use HasUuids;

    protected $table = 'silver.surveys';
    protected $primaryKey = 'survey_id';
    public $incrementing = false;
    protected $keyType = 'string';

    protected $fillable = [
        'collar_id',
        'depth',
        'azimuth',
        'dip',
        'survey_method',
    ];

    protected $casts = [
        'depth'         => 'float',
        'azimuth'       => 'float',
        'dip'           => 'float',
        'created_at'    => 'datetime',
        'updated_at'    => 'datetime',
        // §04e Downhole Survey — closed-vocabulary instrument family.
        'survey_method' => SurveyMethod::class,
    ];

    /**
     * Get the collar this survey belongs to.
     */
    public function collar(): BelongsTo
    {
        return $this->belongsTo(Collar::class, 'collar_id', 'collar_id');
    }
}
