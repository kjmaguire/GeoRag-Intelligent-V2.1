<?php

declare(strict_types=1);

namespace App\Models\Ops;

use App\Models\Project;
use App\Models\User;
use Database\Factories\Ops\SupportTicketFactory;
use Illuminate\Database\Eloquent\Concerns\HasUuids;
use Illuminate\Database\Eloquent\Factories\HasFactory;
use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\BelongsTo;
use Illuminate\Database\Eloquent\Relations\HasMany;

/**
 * Eloquent model for `ops.support_tickets` (master-plan §10.8 /
 * doc-phase 97 schema; doc-phase 109 model layer).
 *
 * Customer-reported issues feeding the §25 Customer Support Cockpit.
 * NO workspace_id RLS — ops.* is global; cross-workspace access logged
 * via `audit_ledger.action_type='support_access'` per §25.3.
 *
 * Categories per §25.2: wrong_answer | failed_ingestion |
 * failed_report | integration_issue | performance | other.
 *
 * Severities: low | medium | high | critical.
 * Statuses: open | investigating | resolved | closed.
 */
class SupportTicket extends Model
{
    /** @use HasFactory<SupportTicketFactory> */
    use HasFactory;
    use HasUuids;

    protected $table = 'ops.support_tickets';

    protected $primaryKey = 'ticket_id';

    public $incrementing = false;

    protected $keyType = 'string';

    public $timestamps = false; // schema uses reported_at + resolved_at, not created_at/updated_at

    protected $fillable = [
        'workspace_id',
        'reported_by_user_id',
        'reported_at',
        'channel',
        'category',
        'description',
        'severity',
        'assigned_to_user_id',
        'status',
        'resolution_summary',
        'resolved_at',
        'customer_visible_response',
    ];

    protected $casts = [
        'reported_at' => 'datetime',
        'resolved_at' => 'datetime',
    ];

    /**
     * The user who reported this ticket (customer; may be null for
     * anonymous channels).
     */
    public function reporter(): BelongsTo
    {
        return $this->belongsTo(User::class, 'reported_by_user_id', 'id');
    }

    /**
     * The ops user this ticket is assigned to (may be null when
     * unassigned).
     */
    public function assignee(): BelongsTo
    {
        return $this->belongsTo(User::class, 'assigned_to_user_id', 'id');
    }

    /**
     * Trace correlations attached to this ticket.
     */
    public function traces(): HasMany
    {
        return $this->hasMany(SupportTicketTrace::class, 'ticket_id', 'ticket_id');
    }

    /**
     * Replay runs initiated against this ticket.
     */
    public function replayRuns(): HasMany
    {
        return $this->hasMany(SupportReplayRun::class, 'ticket_id', 'ticket_id');
    }
}
