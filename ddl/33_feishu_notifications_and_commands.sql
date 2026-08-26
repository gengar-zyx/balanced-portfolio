-- Existing deployments: durable Feishu rebalance notifications and /position commands.
-- Safe to run more than once.

CREATE TABLE IF NOT EXISTS bp_notification_outbox (
    notification_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    channel         TEXT        NOT NULL,
    event_type      TEXT        NOT NULL,
    portfolio_id    BIGINT      NOT NULL REFERENCES bp_portfolio(portfolio_id) ON DELETE CASCADE,
    method          TEXT        NOT NULL,
    trade_date      DATE        NOT NULL,
    payload         JSONB       NOT NULL,
    status          TEXT        NOT NULL DEFAULT 'pending',
    attempts        INTEGER     NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    locked_at       TIMESTAMPTZ,
    sent_at         TIMESTAMPTZ,
    last_error      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_bp_notification_event UNIQUE
        (channel, event_type, portfolio_id, method, trade_date),
    CONSTRAINT ck_bp_notification_status CHECK
        (status IN ('pending', 'sending', 'sent', 'failed', 'exhausted'))
);

CREATE INDEX IF NOT EXISTS idx_bp_notification_due
    ON bp_notification_outbox (next_attempt_at, notification_id)
    WHERE status IN ('pending', 'failed');

CREATE TABLE IF NOT EXISTS bp_feishu_command_event (
    command_event_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id         TEXT        NOT NULL UNIQUE,
    message_id       TEXT        NOT NULL UNIQUE,
    chat_id          TEXT        NOT NULL,
    chat_type        TEXT        NOT NULL,
    command          TEXT        NOT NULL,
    argument         TEXT,
    response_payload JSONB,
    status           TEXT        NOT NULL DEFAULT 'pending',
    attempts         INTEGER     NOT NULL DEFAULT 0,
    next_attempt_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    locked_at        TIMESTAMPTZ,
    replied_at       TIMESTAMPTZ,
    last_error       TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_bp_feishu_command_status CHECK
        (status IN ('pending', 'sending', 'sent', 'failed', 'exhausted'))
);

CREATE INDEX IF NOT EXISTS idx_bp_feishu_command_due
    ON bp_feishu_command_event (next_attempt_at, command_event_id)
    WHERE status IN ('pending', 'failed');
