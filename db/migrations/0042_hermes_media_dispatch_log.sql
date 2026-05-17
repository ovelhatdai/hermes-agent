-- SPEC-074 Task 02 — Tabela de audit/dedup de dispatch de midia
-- 2026-04-20

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS public.hermes_media_dispatch_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform        TEXT NOT NULL,
    chat_id         TEXT NOT NULL,
    media_type      TEXT NOT NULL,
    filename        TEXT NOT NULL,
    mimetype        TEXT NOT NULL,
    size_bytes      BIGINT NOT NULL,
    source_kind     TEXT NOT NULL,
    platform_msg_id TEXT,
    status          TEXT NOT NULL,
    error           TEXT,
    idempotency_key TEXT,
    bridge_attempts SMALLINT NOT NULL DEFAULT 0,
    duration_ms     INTEGER,
    caller_token_id TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT hermes_media_dispatch_log_platform_check
        CHECK (platform IN ('whatsapp')),
    CONSTRAINT hermes_media_dispatch_log_media_type_check
        CHECK (media_type IN ('document', 'audio', 'voice', 'video', 'image', 'animation')),
    CONSTRAINT hermes_media_dispatch_log_source_kind_check
        CHECK (source_kind IN ('base64', 'url', 'file_path')),
    CONSTRAINT hermes_media_dispatch_log_status_check
        CHECK (status IN ('pending', 'sent', 'failed', 'deduplicated')),
    CONSTRAINT hermes_media_dispatch_log_size_bytes_check
        CHECK (size_bytes >= 0 AND size_bytes <= 26214400)
);

ALTER TABLE public.hermes_media_dispatch_log
    ALTER COLUMN id SET DEFAULT gen_random_uuid(),
    ALTER COLUMN platform SET NOT NULL,
    ALTER COLUMN chat_id SET NOT NULL,
    ALTER COLUMN media_type SET NOT NULL,
    ALTER COLUMN filename SET NOT NULL,
    ALTER COLUMN mimetype SET NOT NULL,
    ALTER COLUMN size_bytes SET NOT NULL,
    ALTER COLUMN source_kind SET NOT NULL,
    ALTER COLUMN status SET NOT NULL,
    ALTER COLUMN created_at SET DEFAULT NOW();

ALTER TABLE public.hermes_media_dispatch_log
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ;

UPDATE public.hermes_media_dispatch_log
SET updated_at = created_at
WHERE updated_at IS NULL;

ALTER TABLE public.hermes_media_dispatch_log
    ALTER COLUMN updated_at SET DEFAULT NOW(),
    ALTER COLUMN updated_at SET NOT NULL;

UPDATE public.hermes_media_dispatch_log
SET bridge_attempts = 0
WHERE bridge_attempts IS NULL;

ALTER TABLE public.hermes_media_dispatch_log
    ALTER COLUMN bridge_attempts TYPE SMALLINT USING bridge_attempts::SMALLINT,
    ALTER COLUMN bridge_attempts SET DEFAULT 0,
    ALTER COLUMN bridge_attempts SET NOT NULL,
    ALTER COLUMN duration_ms DROP DEFAULT,
    ALTER COLUMN size_bytes DROP DEFAULT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'hermes_media_dispatch_log_platform_check'
          AND conrelid = 'public.hermes_media_dispatch_log'::regclass
    ) THEN
        ALTER TABLE public.hermes_media_dispatch_log
            ADD CONSTRAINT hermes_media_dispatch_log_platform_check
            CHECK (platform IN ('whatsapp'));
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'hermes_media_dispatch_log_media_type_check'
          AND conrelid = 'public.hermes_media_dispatch_log'::regclass
    ) THEN
        ALTER TABLE public.hermes_media_dispatch_log
            ADD CONSTRAINT hermes_media_dispatch_log_media_type_check
            CHECK (media_type IN ('document', 'audio', 'voice', 'video', 'image', 'animation'));
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'hermes_media_dispatch_log_source_kind_check'
          AND conrelid = 'public.hermes_media_dispatch_log'::regclass
    ) THEN
        ALTER TABLE public.hermes_media_dispatch_log
            ADD CONSTRAINT hermes_media_dispatch_log_source_kind_check
            CHECK (source_kind IN ('base64', 'url', 'file_path'));
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'hermes_media_dispatch_log_status_check'
          AND conrelid = 'public.hermes_media_dispatch_log'::regclass
    ) THEN
        ALTER TABLE public.hermes_media_dispatch_log
            ADD CONSTRAINT hermes_media_dispatch_log_status_check
            CHECK (status IN ('pending', 'sent', 'failed', 'deduplicated'));
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'hermes_media_dispatch_log_size_bytes_check'
          AND conrelid = 'public.hermes_media_dispatch_log'::regclass
    ) THEN
        ALTER TABLE public.hermes_media_dispatch_log
            ADD CONSTRAINT hermes_media_dispatch_log_size_bytes_check
            CHECK (size_bytes >= 0 AND size_bytes <= 26214400);
    END IF;
END
$$;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_class
        WHERE relkind = 'i'
          AND relname = 'idx_hmd_dedup'
    ) AND NOT EXISTS (
        SELECT 1
        FROM pg_class
        WHERE relkind = 'i'
          AND relname = 'idx_hmdl_idempotency'
    ) THEN
        ALTER INDEX idx_hmd_dedup RENAME TO idx_hmdl_idempotency;
    END IF;
END
$$;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_class
        WHERE relkind = 'i'
          AND relname = 'idx_hmd_status'
    ) AND NOT EXISTS (
        SELECT 1
        FROM pg_class
        WHERE relkind = 'i'
          AND relname = 'idx_hmdl_status_created'
    ) THEN
        ALTER INDEX idx_hmd_status RENAME TO idx_hmdl_status_created;
    END IF;
END
$$;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_class
        WHERE relkind = 'i'
          AND relname = 'idx_hmd_created'
    ) AND NOT EXISTS (
        SELECT 1
        FROM pg_class
        WHERE relkind = 'i'
          AND relname = 'idx_hmdl_created'
    ) THEN
        ALTER INDEX idx_hmd_created RENAME TO idx_hmdl_created;
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS idx_hmdl_idempotency
    ON public.hermes_media_dispatch_log (idempotency_key, chat_id, created_at DESC)
    WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_hmdl_chat_created
    ON public.hermes_media_dispatch_log (chat_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_hmdl_status_created
    ON public.hermes_media_dispatch_log (status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_hmdl_created
    ON public.hermes_media_dispatch_log (created_at DESC);

CREATE OR REPLACE FUNCTION public.hermes_media_dispatch_log_touch()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_hmdl_touch ON public.hermes_media_dispatch_log;
CREATE TRIGGER trg_hmdl_touch
    BEFORE UPDATE ON public.hermes_media_dispatch_log
    FOR EACH ROW
    EXECUTE FUNCTION public.hermes_media_dispatch_log_touch();

COMMIT;
