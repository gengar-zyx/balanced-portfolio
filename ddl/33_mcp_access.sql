-- Existing installations: apply once before enabling BP_MCP_ENABLED.
BEGIN;
CREATE TABLE IF NOT EXISTS bp_agent_token (
    token_id UUID PRIMARY KEY,
    owner_user_id BIGINT NOT NULL REFERENCES bp_user(user_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    prefix TEXT NOT NULL,
    scopes TEXT[] NOT NULL DEFAULT ARRAY['read']::TEXT[],
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    last_used_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    CHECK (scopes <@ ARRAY['read','portfolio:write','compute']::TEXT[])
);
CREATE INDEX IF NOT EXISTS idx_bp_agent_token_owner ON bp_agent_token(owner_user_id);
CREATE TABLE IF NOT EXISTS bp_agent_request (
    owner_user_id BIGINT NOT NULL REFERENCES bp_user(user_id) ON DELETE CASCADE,
    tool TEXT NOT NULL,
    request_id TEXT NOT NULL,
    params_hash TEXT NOT NULL,
    response JSONB NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL DEFAULT now() + interval '24 hours',
    PRIMARY KEY (owner_user_id, tool, request_id)
);
CREATE INDEX IF NOT EXISTS idx_bp_agent_request_expiry ON bp_agent_request(expires_at);
ALTER TABLE bp_task ADD COLUMN IF NOT EXISTS initiated_via TEXT;
ALTER TABLE bp_task ADD COLUMN IF NOT EXISTS execution_owner TEXT;
ALTER TABLE bp_task ADD COLUMN IF NOT EXISTS dispatch_payload JSONB;
ALTER TABLE bp_task ADD COLUMN IF NOT EXISTS dispatched_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_bp_task_mcp_owner ON bp_task(owner_user_id, status)
    WHERE initiated_via = 'mcp';
COMMIT;
