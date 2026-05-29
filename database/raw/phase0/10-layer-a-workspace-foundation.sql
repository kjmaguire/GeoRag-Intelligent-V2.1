-- =============================================================================
-- Phase 0 — Layer A delta — workspace_memberships + workspace_roles
--
-- Layer A in the kickoff Step 2 list specifies four tables (users, workspaces,
-- workspace_memberships, workspace_roles). Two already exist with live data
-- and 19+ FK references — do not duplicate (Phase 0 decision #5):
--
--   silver.workspaces  (PK = workspace_id uuid)
--   public.users       (PK = id bigint, Laravel default)
--
-- This file ships only the missing pieces.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- workspace.workspace_roles
--
-- RBAC role definitions. workspace_id NULL ⇒ global role; otherwise
-- workspace-scoped (so customers can define their own roles in addition to
-- platform defaults).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS workspace.workspace_roles (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    uuid        NULL REFERENCES silver.workspaces(workspace_id) ON DELETE CASCADE,
    name            text        NOT NULL,
    description     text        NULL,
    permissions     jsonb       NOT NULL DEFAULT '[]'::jsonb,
    is_system       boolean     NOT NULL DEFAULT false,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT workspace_roles_name_per_scope UNIQUE (workspace_id, name)
);

COMMENT ON TABLE  workspace.workspace_roles IS 'RBAC role definitions; workspace_id NULL = global system role.';
COMMENT ON COLUMN workspace.workspace_roles.permissions IS 'Array of permission strings, e.g. ["audit.read","report.signoff"].';
COMMENT ON COLUMN workspace.workspace_roles.is_system IS 'TRUE for platform-curated roles that customers cannot delete.';

CREATE INDEX IF NOT EXISTS workspace_roles_workspace_id_idx
    ON workspace.workspace_roles (workspace_id) WHERE workspace_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- workspace.workspace_memberships
--
-- User ↔ workspace many-to-many with role. Cross-schema FKs are intentional:
-- public.users is the Laravel default identity table; silver.workspaces holds
-- the live tenant data. Both are immovable per Phase 0 decision #5.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS workspace.workspace_memberships (
    id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         bigint      NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    workspace_id    uuid        NOT NULL REFERENCES silver.workspaces(workspace_id) ON DELETE CASCADE,
    role_id         uuid        NOT NULL REFERENCES workspace.workspace_roles(id) ON DELETE RESTRICT,
    invited_by      bigint      NULL REFERENCES public.users(id) ON DELETE SET NULL,
    invited_at      timestamptz NULL,
    accepted_at     timestamptz NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT workspace_memberships_user_workspace UNIQUE (user_id, workspace_id)
);

COMMENT ON TABLE  workspace.workspace_memberships IS 'User-to-workspace membership with role binding.';
COMMENT ON COLUMN workspace.workspace_memberships.invited_at IS 'NULL for self-created (workspace owner) memberships; set when invitation sent.';
COMMENT ON COLUMN workspace.workspace_memberships.accepted_at IS 'NULL until invitee accepts (or self-creation, set to created_at).';

CREATE INDEX IF NOT EXISTS workspace_memberships_workspace_id_idx
    ON workspace.workspace_memberships (workspace_id);
CREATE INDEX IF NOT EXISTS workspace_memberships_user_id_idx
    ON workspace.workspace_memberships (user_id);

-- ---------------------------------------------------------------------------
-- Seed system roles (idempotent)
-- ---------------------------------------------------------------------------
INSERT INTO workspace.workspace_roles (workspace_id, name, description, permissions, is_system)
VALUES
    (NULL, 'workspace_admin',  'Full administrative control of a workspace.',
        '["workspace.manage","membership.manage","agent.config","audit.read","report.signoff"]'::jsonb, true),
    (NULL, 'workspace_member', 'Standard member: read+write within workspace, no admin.',
        '["workspace.read","workspace.write","report.read","audit.read.own"]'::jsonb, true),
    (NULL, 'workspace_viewer', 'Read-only member: dashboards and reports.',
        '["workspace.read","report.read"]'::jsonb, true)
ON CONFLICT (workspace_id, name) DO NOTHING;
