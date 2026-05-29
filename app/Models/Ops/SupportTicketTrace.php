<?php

declare(strict_types=1);

namespace App\Models\Ops;

use App\Models\User;
use Illuminate\Database\Eloquent\Concerns\HasUuids;
use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\BelongsTo;

/**
 * Eloquent model for `ops.support_ticket_traces` (master-plan §10.8 /
 * doc-phase 97 schema; doc-phase 109 model layer).
 *
 * Many-to-many: support tickets ↔ correlated trace_ids. A single
 * ticket can link to many traces (chat answer + workflow + audit
 * ledger entries that all share the same trace_id).
 */
class SupportTicketTrace extends Model
{
    use HasUuids;

    protected $table = 'ops.support_ticket_traces';

    protected $primaryKey = 'link_id';

    public $incrementing = false;

    protected $keyType = 'string';

    public $timestamps = false;

    protected $fillable = [
        'ticket_id',
        'trace_id',
        'trace_summary',
        'added_by_user_id',
        'added_at',
    ];

    protected $casts = [
        'added_at' => 'datetime',
    ];

    public function ticket(): BelongsTo
    {
        return $this->belongsTo(SupportTicket::class, 'ticket_id', 'ticket_id');
    }

    public function addedBy(): BelongsTo
    {
        return $this->belongsTo(User::class, 'added_by_user_id', 'id');
    }
}
