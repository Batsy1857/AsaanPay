"""Deterministic sandbox engines: no I/O, artificial waits, or shared state."""

import re
from decimal import Decimal

from app.contracts import IssuerResult, RiskReason, RiskResult, RiskStatus

MERCHANT_COUNTRY = "PK"
HIGH_VALUE_THRESHOLD = Decimal("50000")
MAX_AMOUNT_PKR = Decimal("999999999999.99")


async def simulate_issuer(card_preset: str) -> IssuerResult:
    """Accept only synthetic presets; unsupported input is a validation error."""
    if card_preset == "approved":
        return IssuerResult.APPROVED
    if card_preset == "declined":
        return IssuerResult.DECLINED
    raise ValueError("card_preset must be approved or declined")


async def screen_risk(amount_pkr: Decimal, billing_country: str) -> RiskResult:
    """Screen exact decimal PKR and normalize a two-letter billing country.

    Decimal is required to avoid silently importing binary float rounding. Invalid
    inputs raise TypeError/ValueError; they are never converted into a CLEAR result.
    """
    if not isinstance(amount_pkr, Decimal):
        raise TypeError("amount_pkr must be a Decimal")
    if not amount_pkr.is_finite() or not Decimal("0") < amount_pkr <= MAX_AMOUNT_PKR:
        raise ValueError("amount_pkr must be positive and within NUMERIC(14,2) bounds")
    # Inspect digits rather than rounding, independent of the decimal context.
    _, digits, exponent = amount_pkr.as_tuple()
    if exponent < -2 and any(digits[exponent + 2:]):
        raise ValueError("amount_pkr must have at most two fractional digits")
    if not isinstance(billing_country, str) or not re.fullmatch(r"[A-Za-z]{2}", billing_country):
        raise ValueError("billing_country must contain two ASCII letters")
    country = billing_country.upper()

    reasons = []
    explanations = []
    if amount_pkr >= HIGH_VALUE_THRESHOLD:
        reasons.append(RiskReason.HIGH_VALUE)
        explanations.append(f"Amount {amount_pkr} PKR meets the 50000 PKR threshold.")
    if country != MERCHANT_COUNTRY:
        reasons.append(RiskReason.CROSS_BORDER)
        explanations.append(f"Billing country {country} differs from merchant country PK.")
    if not reasons:
        explanations.append(f"Amount {amount_pkr} PKR is below 50000 PKR and billing country is PK.")
    return RiskResult(
        status=RiskStatus.VERIFY if reasons else RiskStatus.CLEAR,
        reasons=tuple(reasons),
        explanation=" ".join(explanations),
    )
