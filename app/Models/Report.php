<?php

declare(strict_types=1);

namespace App\Models;

use Illuminate\Database\Eloquent\Concerns\HasUuids;
use Illuminate\Database\Eloquent\Model;

class Report extends Model
{
    use HasUuids;

    protected $table = 'silver.reports';
    protected $primaryKey = 'report_id';
    public $incrementing = false;
    protected $keyType = 'string';

    protected $fillable = [
        'title',
        'authors',
        'company',
        'filing_date',
        'commodity',
        'project_name',
        'region',
        'resource_estimate',
        'sections_text',
        'embedding_ids',
    ];

    protected $casts = [
        'filing_date' => 'date',
        'authors' => 'array',
        'resource_estimate' => 'array',
        'sections_text' => 'array',
        'embedding_ids' => 'array',
        'created_at' => 'datetime',
        'updated_at' => 'datetime',
    ];
}
