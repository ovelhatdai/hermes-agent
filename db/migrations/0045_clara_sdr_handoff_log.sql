BEGIN;
CREATE TABLE IF NOT EXISTS public.clara_sdr_handoff_log (
  id BIGSERIAL PRIMARY KEY,
  conv_id TEXT NOT NULL,
  lead_phone TEXT NOT NULL,
  source TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_clara_sdr_handoff_phone ON public.clara_sdr_handoff_log(lead_phone);
COMMIT;
