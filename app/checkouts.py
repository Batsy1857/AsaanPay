"""Validated checkout resources and idempotent orchestration ownership."""

import asyncio
import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from app import storage
from app.orchestration import orchestrate_payment
from app.simulators import MAX_AMOUNT_PKR

router = APIRouter()


class CheckoutInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount_pkr: Decimal
    card_preset: Literal["approved", "declined"]
    billing_country: str
    execution_mode: Literal["sequential", "concurrent"]
    workload_profile: Literal["lightweight", "balanced", "asymmetric"] = "lightweight"

    @field_validator("amount_pkr", mode="before")
    @classmethod
    def money(cls, value):
        if isinstance(value, bool) or not isinstance(value, (int, str, Decimal)):
            raise ValueError("amount_pkr must be an exact decimal amount")
        try:
            amount = Decimal(value)
        except (InvalidOperation, ValueError):
            raise ValueError("Invalid decimal amount") from None
        if not amount.is_finite() or not Decimal(0) < amount <= MAX_AMOUNT_PKR:
            raise ValueError("amount_pkr is outside supported bounds")
        _, digits, exponent = amount.as_tuple()
        if exponent < -2 and any(digits[exponent + 2:]):
            raise ValueError("amount_pkr must have at most two fractional digits")
        return Decimal(format(amount, ".2f"))

    @field_validator("billing_country")
    @classmethod
    def country(cls, value):
        if not re.fullmatch(r"[A-Za-z]{2}", value):
            raise ValueError("billing_country must contain two ASCII letters")
        return value.upper()

    def canonical(self):
        return {**self.model_dump(mode="json"), "amount_pkr": format(self.amount_pkr, ".2f")}

    def fingerprint(self):
        payload = json.dumps(self.canonical(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def get_orchestrator():
    """Dependency override is an internal test seam, never an HTTP input."""
    return orchestrate_payment


def resource(row):
    trace = row["timing_trace"]
    if isinstance(trace, str):
        trace = json.loads(trace)
    return {
        "id": str(row["id"]), "state": row["state"],
        "terminal": row["state"] in storage.TERMINAL_STATES,
        "request": {
            "amount_pkr": format(row["amount_pkr"], ".2f"),
            **{key: row[key] for key in ("card_preset", "billing_country", "execution_mode", "workload_profile")},
        },
        **{key: row[key] for key in (
            "rule_set_version", "issuer_result", "risk_result", "decision", "decision_reason",
            "error_reason", "created_at", "processing_started_at", "finished_at",
        )},
        "timing_trace": trace,
    }


def response(row, status):
    headers = {"Location": f"/api/checkouts/{row['id']}"}
    if status == 202:
        headers["Retry-After"] = "1"
    return JSONResponse(status_code=status, content=jsonable_encoder(resource(row)), headers=headers)


def reject_constant(value):
    raise ValueError("Nonfinite JSON number")


@router.post("/api/checkouts")
async def checkout(request: Request, orchestrator=Depends(get_orchestrator)):
    keys = request.headers.getlist("idempotency-key")
    if len(keys) != 1 or re.fullmatch(r"[!-~]{1,128}", keys[0]) is None:
        raise HTTPException(422, "A single printable Idempotency-Key (1–128 characters) is required")
    try:
        # Parse numeric JSON tokens directly to Decimal, before Pydantic validation.
        raw = json.loads(await request.body(), parse_float=Decimal, parse_constant=reject_constant)
        payment = CheckoutInput.model_validate(raw)
    except (ValueError, UnicodeError, ValidationError):
        raise HTTPException(422, "Invalid checkout inputs") from None

    pool = request.app.state.pool
    try:
        row, owner = await storage.claim(pool, keys[0], payment.fingerprint(), payment)
        if not owner:
            if row["request_fingerprint"] != payment.fingerprint() or resource(row)["request"] != payment.canonical():
                raise HTTPException(409, "Idempotency key already used with a different payload")
            return response(row, 200 if row["state"] in storage.TERMINAL_STATES else 202)

        attempt_id = row["id"]
        try:
            await storage.start_processing(pool, attempt_id)
            try:
                result = await orchestrator(**payment.model_dump())
            except asyncio.CancelledError:
                raise
            except Exception:
                row = await storage.fail(pool, attempt_id, "ORCHESTRATION_ERROR: execution failed")
            else:
                row = await storage.finish(pool, attempt_id, result)
            if row is None:
                raise storage.StorageError("Final state could not be confirmed")
        except asyncio.CancelledError:
            # The orchestrator has already joined/cancelled its owned tasks. No
            # shielding or detached cleanup task: repeated cancellation may abort
            # this write, leaving startup recovery to mark the unfinished row.
            try:
                await storage.fail(pool, attempt_id, "REQUEST_CANCELLED: execution was cancelled")
            except storage.StorageError:
                pass
            raise
        return response(row, 201)
    except storage.StorageError:
        raise HTTPException(503, "Database unavailable; retry with the same idempotency key") from None


@router.get("/api/checkouts/{attempt_id}")
async def get_checkout(attempt_id: UUID, request: Request):
    try:
        row = await storage.get_attempt(request.app.state.pool, attempt_id)
    except storage.StorageError:
        raise HTTPException(503, "Database unavailable") from None
    if row is None:
        raise HTTPException(404, "Checkout not found")
    return resource(row)
