-- Applied by the PostgreSQL image only when its data directory is empty.
CREATE TABLE payment_attempts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    idempotency_key VARCHAR(128) NOT NULL UNIQUE
        CHECK (idempotency_key ~ '^[!-~]{1,128}$'),
    request_fingerprint TEXT NOT NULL
        CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
    amount_pkr NUMERIC(14,2) NOT NULL CHECK (amount_pkr > 0 AND amount_pkr < 1000000000000),
    card_preset TEXT NOT NULL CHECK (card_preset IN ('approved', 'declined')),
    billing_country TEXT NOT NULL CHECK (billing_country ~ '^[A-Z]{2}$'),
    execution_mode TEXT NOT NULL CHECK (execution_mode IN ('sequential', 'concurrent')),
    workload_profile TEXT NOT NULL DEFAULT 'lightweight'
        CHECK (workload_profile IN ('lightweight', 'balanced', 'asymmetric')),
    state TEXT NOT NULL DEFAULT 'CREATED'
        CHECK (state IN ('CREATED', 'PROCESSING', 'SUCCEEDED', 'DECLINED', 'VERIFICATION_REQUIRED', 'ERROR')),
    issuer_result TEXT CHECK (issuer_result IN ('APPROVED', 'DECLINED')),
    risk_result TEXT CHECK (risk_result IN ('CLEAR', 'VERIFY')),
    decision TEXT CHECK (decision IN ('SUCCEEDED', 'DECLINED', 'VERIFICATION_REQUIRED')),
    decision_reason TEXT,
    rule_set_version TEXT NOT NULL DEFAULT 'sandbox_v1',
    timing_trace JSONB CHECK (jsonb_typeof(timing_trace) = 'object'),
    error_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    processing_started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    CHECK (decision IS NULL OR decision = state),
    CHECK (state <> 'ERROR' OR (error_reason IS NOT NULL AND decision IS NULL))
);
