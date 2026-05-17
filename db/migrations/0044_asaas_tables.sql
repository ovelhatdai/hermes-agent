BEGIN;

CREATE TABLE IF NOT EXISTS public.asaas_event_log (
    id BIGSERIAL PRIMARY KEY,
    asaas_event_id TEXT UNIQUE NOT NULL,
    event_type TEXT NOT NULL,
    payment_id TEXT,
    customer_id TEXT,
    amount NUMERIC(10,2),
    payload JSONB NOT NULL,
    processed_at TIMESTAMPTZ,
    zapsign_doc_id TEXT,
    notification_sent BOOLEAN NOT NULL DEFAULT false,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_asaas_event_log_payment
    ON public.asaas_event_log (payment_id);

CREATE INDEX IF NOT EXISTS idx_asaas_event_log_event_type
    ON public.asaas_event_log (event_type);

CREATE INDEX IF NOT EXISTS idx_asaas_event_log_processed
    ON public.asaas_event_log (processed_at)
    WHERE processed_at IS NULL;

CREATE TABLE IF NOT EXISTS public.asaas_payment_request (
    id BIGSERIAL PRIMARY KEY,
    agent_source TEXT NOT NULL,
    conv_id TEXT,
    lead_phone TEXT NOT NULL,
    lead_name TEXT NOT NULL,
    lead_cpf TEXT NOT NULL,
    lead_email TEXT,
    sku TEXT NOT NULL,
    amount NUMERIC(10,2) NOT NULL,
    installments INTEGER,
    asaas_customer_id TEXT,
    asaas_payment_id TEXT,
    invoice_url TEXT,
    status TEXT NOT NULL DEFAULT 'requested',
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_asaas_payment_request_phone
    ON public.asaas_payment_request (lead_phone);

CREATE INDEX IF NOT EXISTS idx_asaas_payment_request_status
    ON public.asaas_payment_request (status);

CREATE INDEX IF NOT EXISTS idx_asaas_payment_request_payment_id
    ON public.asaas_payment_request (asaas_payment_id);

COMMIT;
