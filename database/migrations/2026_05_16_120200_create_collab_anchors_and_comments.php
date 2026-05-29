<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * silver.collab_anchors + silver.collab_comments — A.3 Team Collaboration
 * primitives. Anchors comments/mentions/review-requests to one of three
 * target kinds: answer_run | map_feature | document.
 */
return new class extends Migration {
    public function up(): void
    {
        DB::statement('SET search_path TO silver, public;');

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.collab_anchors (
                anchor_id     UUID         NOT NULL DEFAULT gen_random_uuid(),
                workspace_id  UUID         NOT NULL,
                target_kind   VARCHAR(20)  NOT NULL,
                target_id     TEXT         NOT NULL,
                project_id    UUID         NULL,
                created_by    BIGINT       NOT NULL,
                created_at    TIMESTAMP(0) WITHOUT TIME ZONE,
                updated_at    TIMESTAMP(0) WITHOUT TIME ZONE,
                CONSTRAINT collab_anchors_pkey PRIMARY KEY (anchor_id),
                CONSTRAINT collab_anchors_target_kind_valid
                    CHECK (target_kind IN ('answer_run', 'map_feature', 'document'))
            )
        SQL);

        DB::statement('CREATE INDEX IF NOT EXISTS collab_anchors_target_idx ON silver.collab_anchors (target_kind, target_id)');
        DB::statement('CREATE INDEX IF NOT EXISTS collab_anchors_workspace_idx ON silver.collab_anchors (workspace_id)');

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.collab_comments (
                comment_id    UUID         NOT NULL DEFAULT gen_random_uuid(),
                anchor_id     UUID         NOT NULL,
                parent_comment_id UUID     NULL,
                body          TEXT         NOT NULL,
                mentions      BIGINT[]     NOT NULL DEFAULT '{}',
                resolved      BOOLEAN      NOT NULL DEFAULT false,
                created_by    BIGINT       NOT NULL,
                created_at    TIMESTAMP(0) WITHOUT TIME ZONE,
                updated_at    TIMESTAMP(0) WITHOUT TIME ZONE,
                CONSTRAINT collab_comments_pkey PRIMARY KEY (comment_id),
                CONSTRAINT collab_comments_anchor_fkey
                    FOREIGN KEY (anchor_id)
                    REFERENCES silver.collab_anchors (anchor_id)
                    ON DELETE CASCADE,
                CONSTRAINT collab_comments_parent_fkey
                    FOREIGN KEY (parent_comment_id)
                    REFERENCES silver.collab_comments (comment_id)
                    ON DELETE CASCADE
            )
        SQL);

        DB::statement('CREATE INDEX IF NOT EXISTS collab_comments_anchor_idx ON silver.collab_comments (anchor_id)');
        DB::statement('CREATE INDEX IF NOT EXISTS collab_comments_unresolved_idx ON silver.collab_comments (anchor_id) WHERE resolved = false');
    }

    public function down(): void
    {
        DB::statement('DROP TABLE IF EXISTS silver.collab_comments');
        DB::statement('DROP TABLE IF EXISTS silver.collab_anchors');
    }
};
