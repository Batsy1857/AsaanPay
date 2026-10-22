"""Milestone 1: application lifecycle and database connectivity only."""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        pool = await asyncpg.create_pool(
            host=os.getenv("POSTGRES_HOST", "localhost"),
            port=int(os.getenv("POSTGRES_PORT", "5432")),
            user=os.environ["POSTGRES_USER"],
            password=os.environ["POSTGRES_PASSWORD"],
            database=os.environ["POSTGRES_DB"],
            min_size=1,
            max_size=5,
            timeout=3,
            command_timeout=3,
        )
    except (OSError, asyncpg.PostgresError, TimeoutError, KeyError, ValueError) as exc:
        # Exception messages/tracebacks can contain credentials or a supplied URI.
        # Log structured diagnostics instead; never log raw environment values.
        host = os.getenv("POSTGRES_HOST", "localhost")
        port = os.getenv("POSTGRES_PORT", "5432")
        logger.error(
            "PostgreSQL startup failed: exception=%s sqlstate=%s errno=%s "
            "host=%s port=%s connect_timeout=3s missing_settings=%s "
            "DATABASE_URL_present=%s (unused; expected POSTGRES_* settings)",
            type(exc).__name__,
            getattr(exc, "sqlstate", None),
            getattr(exc, "errno", None),
            host if host in ("db", "localhost", "127.0.0.1", "::1") else "<custom host omitted>",
            "5432" if port == "5432" else "<custom or invalid port omitted>",
            [key for key in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB")
             if not os.getenv(key)],
            bool(os.getenv("DATABASE_URL")),
        )
        raise RuntimeError(
            "PostgreSQL startup failed: check database availability and POSTGRES_* settings."
        ) from None
    app.state.pool = pool
    try:
        yield
    finally:
        try:
            await asyncio.wait_for(pool.close(), timeout=5)
        except TimeoutError:
            pool.terminate()


app = FastAPI(title="AsaanPay", lifespan=lifespan)


@app.get("/api/health")
async def health(request: Request):
    try:
        # Bound pool acquisition as well as the query itself.
        async with asyncio.timeout(3):
            value = await request.app.state.pool.fetchval("SELECT 1")
        if value != 1:
            raise RuntimeError("Unexpected database probe result")
    except (OSError, asyncpg.PostgresError, asyncpg.InterfaceError, TimeoutError, RuntimeError):
        logger.warning("PostgreSQL health check failed")
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "database": "unavailable"},
        )
    return {"status": "ok", "database": "connected"}
