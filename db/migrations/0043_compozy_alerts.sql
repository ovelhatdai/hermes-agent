-- SPEC-077 Task 08 — Hermes watcher alert log + quiet-hours buffer
-- 2026-04-22

BEGIN;

CREATE TABLE IF NOT EXISTS public.compozy_alert_log (
    id BIGSERIAL PRIMARY KEY,
    run_id TEXT NOT NULL,
    spec_id TEXT NOT NULL,
    milestone TEXT NOT NULL,
    sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (run_id, milestone)
);

CREATE INDEX IF NOT EXISTS idx_compozy_alert_log_spec_id
    ON public.compozy_alert_log (spec_id);

CREATE INDEX IF NOT EXISTS idx_compozy_alert_log_sent_at
    ON public.compozy_alert_log (sent_at DESC);

CREATE TABLE IF NOT EXISTS public.compozy_alert_buffer (
    id BIGSERIAL PRIMARY KEY,
    run_id TEXT NOT NULL,
    spec_id TEXT NOT NULL,
    milestone TEXT NOT NULL,
    milestone_type TEXT NOT NULL,
    body TEXT NOT NULL,
    release_after TIMESTAMPTZ NOT NULL,
    buffered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    UNIQUE (run_id, milestone)
);

CREATE INDEX IF NOT EXISTS idx_compozy_alert_buffer_release_after
    ON public.compozy_alert_buffer (release_after ASC, buffered_at ASC);

CREATE INDEX IF NOT EXISTS idx_compozy_alert_buffer_spec_id
    ON public.compozy_alert_buffer (spec_id);

COMMIT;
