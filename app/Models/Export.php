<?php

declare(strict_types=1);

namespace App\Models;

use Illuminate\Database\Eloquent\Concerns\HasUuids;
use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\BelongsTo;

/**
 * A data export request and, once finished, where its file lives.
 *
 * `silver.exports.download_url` and `download_url_expires_at` are LEGACY
 * columns: before 2026-10-10 GenerateExportJob stored a 24-hour presigned URL
 * in them. Production signs with ECS task-role session credentials, so that URL
 * died with the session and carried a session token long enough to overflow the
 * old varchar(1000) column. Nothing writes or reads them any more; the API
 * mints a short-lived URL from `minio_path` per response (see ExportController)
 * and the columns are kept only so a worker still running the previous release
 * cannot fail against a missing column. Drop them once none is.
 */
class Export extends Model
{
    use HasUuids;

    protected $table = 'silver.exports';

    protected $primaryKey = 'export_id';

    public $incrementing = false;

    protected $keyType = 'string';

    protected $fillable = [
        'project_id',
        'workspace_id',
        'export_type',
        'status',
        'format',
        'filters',
        'file_count',
        'total_size_bytes',
        'minio_path',
        'error_message',
        'completed_at',
    ];

    protected $casts = [
        'filters' => 'array',
        'file_count' => 'integer',
        'total_size_bytes' => 'integer',
        'completed_at' => 'datetime',
        'created_at' => 'datetime',
        'updated_at' => 'datetime',
    ];

    /**
     * The project this export belongs to.
     */
    public function project(): BelongsTo
    {
        return $this->belongsTo(Project::class, 'project_id', 'project_id');
    }
}
