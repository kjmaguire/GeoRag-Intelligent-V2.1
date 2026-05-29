<?php

declare(strict_types=1);

namespace App\Models;

use Illuminate\Database\Eloquent\Concerns\HasUuids;
use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\BelongsTo;

class Export extends Model
{
    use HasUuids;

    protected $table = 'silver.exports';
    protected $primaryKey = 'export_id';
    public $incrementing = false;
    protected $keyType = 'string';

    protected $fillable = [
        'project_id',
        'export_type',
        'status',
        'format',
        'filters',
        'file_count',
        'total_size_bytes',
        'minio_path',
        'download_url',
        'download_url_expires_at',
        'error_message',
        'completed_at',
    ];

    protected $casts = [
        'filters'                 => 'array',
        'file_count'              => 'integer',
        'total_size_bytes'        => 'integer',
        'download_url_expires_at' => 'datetime',
        'completed_at'            => 'datetime',
        'created_at'              => 'datetime',
        'updated_at'              => 'datetime',
    ];

    /**
     * The project this export belongs to.
     */
    public function project(): BelongsTo
    {
        return $this->belongsTo(Project::class, 'project_id', 'project_id');
    }
}
