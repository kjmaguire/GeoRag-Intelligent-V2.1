<?php

declare(strict_types=1);

namespace App\Models\Ops;

use App\Models\User;
use Illuminate\Database\Eloquent\Concerns\HasUuids;
use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\BelongsTo;

/**
 * Eloquent model for `ops.support_replay_runs` (master-plan §10.8 /
 * doc-phase 97 schema; doc-phase 109 model layer).
 *
 * Workflow replay attempts for ticket diagnosis. `dry_run` defaults
 * to true; live replays require explicit operator + workspace-owner
 * consent per §25.1. Statuses: pending | running | completed |
 * failed | aborted.
 */
class SupportReplayRun extends Model
{
    use HasUuids;

    protected $table = 'ops.support_replay_runs';

    protected $primaryKey = 'replay_id';

    public $incrementing = false;

    protected $keyType = 'string';

    public $timestamps = false;

    protected $fillable = [
        'ticket_id',
        'original_workflow_run_id',
        'replay_workflow_run_id',
        'diff_summary',
        'dry_run',
        'initiated_by_user_id',
        'initiated_at',
        'completed_at',
        'status',
    ];

    protected $casts = [
        'dry_run' => 'boolean',
        'initiated_at' => 'datetime',
        'completed_at' => 'datetime',
    ];

    public function ticket(): BelongsTo
    {
        return $this->belongsTo(SupportTicket::class, 'ticket_id', 'ticket_id');
    }

    public function initiatedBy(): BelongsTo
    {
        return $this->belongsTo(User::class, 'initiated_by_user_id', 'id');
    }
}
