import { useState } from 'react';
import { router } from '@inertiajs/react';
import { Pill, Modal } from './primitives';

/**
 * CollabPrimitives — comments / @mentions / review-requests anchored to one of:
 *   target_kind ∈ { 'answer_run', 'map_feature', 'document' }
 *
 * Persists to silver.collab_anchors + silver.collab_comments (Wave 0 migrations).
 */

interface CollabAnchorProps {
    target_kind: 'answer_run' | 'map_feature' | 'document';
    target_id: string;
    comments: CollabComment[];
    members?: Array<{ id: number; handle: string; name: string }>;
}

interface CollabComment {
    comment_id: string;
    body: string;
    mentions: number[];
    resolved: boolean;
    created_by: string;
    created_at: string;
}

export function CollabAffordance({ target_kind, target_id, comments, members = [] }: CollabAnchorProps) {
    const [open, setOpen] = useState(false);
    const unresolved = comments.filter((c) => !c.resolved);

    return (
        <>
            <button
                type="button"
                onClick={() => setOpen(true)}
                className="inline-flex items-center gap-1.5 px-2 py-1 text-[10px] font-mono uppercase tracking-wider rounded border"
                style={{ color: unresolved.length > 0 ? 'var(--warn)' : 'var(--fg-2)', borderColor: 'var(--line-2)' }}
            >
                <span>💬</span>
                <span>{comments.length}</span>
                {unresolved.length > 0 && <Pill tone="warn">{unresolved.length}</Pill>}
            </button>
            <Modal open={open} onClose={() => setOpen(false)} maxWidth={520} label="Comments">
                <CommentThread target_kind={target_kind} target_id={target_id} comments={comments} members={members} onClose={() => setOpen(false)} />
            </Modal>
        </>
    );
}

function CommentThread({ target_kind, target_id, comments, members, onClose }: CollabAnchorProps & { onClose: () => void }) {
    const [body, setBody] = useState('');

    function submit() {
        router.post(`/collab/${target_kind}/${target_id}/comments`, { body }, {
            preserveScroll: true,
            onSuccess: () => setBody(''),
        });
    }

    return (
        <div className="flex flex-col h-full">
            <header className="px-4 py-3 border-b flex items-center" style={{ borderColor: 'var(--line-1)' }}>
                <div className="flex-1">
                    <div className="text-[10px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>{target_kind} · {target_id.slice(0, 16)}</div>
                    <div className="text-sm font-medium mt-0.5" style={{ color: 'var(--fg-0)' }}>Comments · {comments.length}</div>
                </div>
                <button type="button" onClick={onClose} className="text-[10px] font-mono uppercase tracking-wider px-2 py-1 rounded border" style={{ color: 'var(--fg-2)', borderColor: 'var(--line-2)' }}>Close</button>
            </header>
            <div className="flex-1 overflow-y-auto p-4 space-y-3 max-h-80">
                {comments.length === 0 ? (
                    <div className="text-xs text-center py-4" style={{ color: 'var(--fg-3)' }}>No comments yet. Start the thread.</div>
                ) : comments.map((c) => (
                    <div key={c.comment_id} className="p-3 rounded border" style={{ background: 'var(--bg-2)', borderColor: 'var(--line-1)' }}>
                        <div className="flex items-center gap-2 mb-1">
                            <span className="text-[10px] font-mono" style={{ color: 'var(--accent)' }}>{c.created_by}</span>
                            <span className="text-[10px] font-mono ml-auto" style={{ color: 'var(--fg-3)' }}>{c.created_at.slice(0, 16)}</span>
                            {c.resolved && <Pill tone="accent" dot>resolved</Pill>}
                        </div>
                        <div className="text-xs" style={{ color: 'var(--fg-1)' }}>{c.body}</div>
                        {c.mentions.length > 0 && (
                            <div className="text-[10px] mt-1 font-mono" style={{ color: 'var(--fg-3)' }}>
                                @{c.mentions.length} mentioned
                            </div>
                        )}
                    </div>
                ))}
            </div>
            <footer className="px-4 py-3 border-t" style={{ borderColor: 'var(--line-1)' }}>
                <textarea
                    value={body}
                    onChange={(e) => setBody(e.target.value)}
                    placeholder="Add a comment… type @ to mention a teammate"
                    rows={2}
                    className="w-full text-xs px-2 py-1.5 rounded border resize-none"
                    style={{ background: 'var(--bg-2)', color: 'var(--fg-0)', borderColor: 'var(--line-2)' }}
                />
                <div className="flex justify-between items-center mt-2">
                    <span className="text-[10px] font-mono uppercase tracking-wider" style={{ color: 'var(--fg-3)' }}>
                        {(members ?? []).length} teammates available
                    </span>
                    <button type="button" onClick={submit} disabled={!body.trim()} className="text-[10px] font-mono uppercase tracking-wider px-3 py-1 rounded border disabled:opacity-40" style={{ color: 'var(--accent)', background: 'var(--accent-bg)', borderColor: 'var(--accent-dim)' }}>
                        Post
                    </button>
                </div>
            </footer>
        </div>
    );
}
