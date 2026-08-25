<?php

declare(strict_types=1);

namespace App\Models;

use App\Casts\TolerantSurveyMethod;
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
        'depth' => 'float',
        'azimuth' => 'float',
        'dip' => 'float',
        'created_at' => 'datetime',
        'updated_at' => 'datetime',
        // §04e Downhole Survey — closed-vocabulary instrument family.
        // Cast through TolerantSurveyMethod, not the enum directly: the
        // ingestion writes 'unknown' for any sheet that names no
        // instrument, and SurveyMethod::from() on that throws a
        // ValueError that CollarController::show turns into a 500 for
        // every collar in the project. See that class for why the
        // vocabulary is not simply widened.
        'survey_method' => TolerantSurveyMethod::class,
    ];

    /**
     * Get the collar this survey belongs to.
     */
    public function collar(): BelongsTo
    {
        return $this->belongsTo(Collar::class, 'collar_id', 'collar_id');
    }
}
