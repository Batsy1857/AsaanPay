"""Short, autocommitted statements; never retain a connection during simulation."""

import asyncio
import json
import logging

import asyncpg

from app.contracts import IssuerResult, RiskResult

logger = logging.getLogger(__name__)
TERMINAL_STATES = {"SUCCEEDED", "DECLINED", "VERIFICATION_REQUIRED", "ERROR"}


class StorageError(Exception):
    """Durable database state could not be confirmed."""


async def query(pool, method, sql, *args):
    try:
        # Pool methods acquire and release their own connection for this statement.
        async with asyncio.timeout(5):
            return await getattr(pool, method)(sql, *args)
    except (OSError, asyncpg.PostgresError, asyncpg.InterfaceError, TimeoutError) as exc:
        logger.warning("Checkout storage unavailable: exception=%s", type(exc).__name__)
        raise StorageError("Database operation could not be confirmed") from None


async def recover_unfinished(pool):
    """Only safe before serving traffic in exactly one API instance."""
    await query(pool, "execute", """
        UPDATE payment_attempts SET state='ERROR', decision=NULL,
            decision_reason=NULL, error_reason='PROCESS_INTERRUPTED: previous execution was interrupted',
            finished_at=CURRENT_TIMESTAMP
        WHERE state IN ('CREATED', 'PROCESSING')
    """)


async def claim(pool, key, fingerprint, payment):
    row = await query(pool, "fetchrow", """
        INSERT INTO payment_attempts
            (idempotency_key, request_fingerprint, amount_pkr, card_preset,
             billing_country, execution_mode, workload_profile)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (idempotency_key) DO NOTHING RETURNING *
    """, key, fingerprint, payment.amount_pkr, payment.card_preset,
        payment.billing_country, payment.execution_mode, payment.workload_profile)
    if row is not None:
        return row, True
    # A separate statement gets a fresh READ COMMITTED snapshot after a rival insert.
    row = await query(pool, "fetchrow",
                      "SELECT * FROM payment_attempts WHERE idempotency_key=$1", key)
    if row is None:
        raise StorageError("Existing attempt unavailable")
    return row, False


async def get_attempt(pool, attempt_id):
    return await query(pool, "fetchrow", "SELECT * FROM payment_attempts WHERE id=$1", attempt_id)


async def start_processing(pool, attempt_id):
    row = await query(pool, "fetchrow", """
        UPDATE payment_attempts SET state='PROCESSING', processing_started_at=CURRENT_TIMESTAMP
        WHERE id=$1 AND state='CREATED' RETURNING *
    """, attempt_id)
    if row is None:
        raise StorageError("Attempt no longer eligible for processing")
    return row


async def finish(pool, attempt_id, result):
    issuer = result.issuer_result.value if isinstance(result.issuer_result, IssuerResult) else None
    risk = result.risk_result.status.value if isinstance(result.risk_result, RiskResult) else None
    error = None if result.decision is not None else f"{result.outcome.reason_code}: {result.explanation}"
    row = await query(pool, "fetchrow", """
        UPDATE payment_attempts SET state=$2, issuer_result=$3, risk_result=$4,
            decision=$5, decision_reason=$6, error_reason=$7, timing_trace=$8::jsonb,
            finished_at=CURRENT_TIMESTAMP
        WHERE id=$1 AND state='PROCESSING' RETURNING *
    """, attempt_id, result.outcome.state.value, issuer, risk,
        result.decision.value if result.decision is not None else None,
        result.explanation if result.decision is not None else None,
        error, json.dumps(result.timing_trace, allow_nan=False))
    if row is None:
        raise StorageError("Attempt no longer eligible for finalization")
    return row


async def fail(pool, attempt_id, reason):
    return await query(pool, "fetchrow", """
        UPDATE payment_attempts SET state='ERROR', decision=NULL, decision_reason=NULL,
            error_reason=$2, finished_at=CURRENT_TIMESTAMP
        WHERE id=$1 AND state IN ('CREATED', 'PROCESSING') RETURNING *
    """, attempt_id, reason)
