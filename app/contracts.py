"""Immutable, framework-independent contracts for the synthetic engines."""

from dataclasses import dataclass
from enum import StrEnum


class IssuerResult(StrEnum):
    APPROVED = "APPROVED"
    DECLINED = "DECLINED"


class RiskStatus(StrEnum):
    CLEAR = "CLEAR"
    VERIFY = "VERIFY"


class RiskReason(StrEnum):
    HIGH_VALUE = "HIGH_VALUE"
    CROSS_BORDER = "CROSS_BORDER"


@dataclass(frozen=True)
class RiskResult:
    status: RiskStatus
    reasons: tuple[RiskReason, ...]
    explanation: str


class BranchFailure(StrEnum):
    """Technical outcomes, separate from valid business results.

    A future orchestrator translates caught exceptions/timeouts into these markers.
    Raw exceptions and their potentially sensitive messages are not decision inputs.
    """

    TECHNICAL_ERROR = "TECHNICAL_ERROR"
    TIMEOUT = "TIMEOUT"


class DecisionState(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    DECLINED = "DECLINED"
    VERIFICATION_REQUIRED = "VERIFICATION_REQUIRED"
    ERROR = "ERROR"


@dataclass(frozen=True)
class Decision:
    state: DecisionState
    reason_code: str
    reason: str
