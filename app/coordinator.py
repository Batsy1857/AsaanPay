"""Pure decision joining; no scheduling, database, clock, or HTTP dependencies."""

from app.contracts import (
    BranchFailure, Decision, DecisionState, IssuerResult, RiskResult, RiskStatus,
)


def coordinate_decision(
    issuer: IssuerResult | BranchFailure | None,
    risk: RiskResult | BranchFailure | None,
) -> Decision:
    """Join results with technical failure precedence and stable issuer-first errors.

    None represents an unavailable result (for example, a branch not yet started
    when its peer failed). Missing/invalid results cannot produce a business outcome.
    """
    for name, result in (("Issuer", issuer), ("Risk", risk)):
        if isinstance(result, BranchFailure):
            if result is BranchFailure.TIMEOUT:
                return Decision(DecisionState.ERROR, "BRANCH_TIMEOUT", f"{name} branch timed out.")
            return Decision(DecisionState.ERROR, "BRANCH_TECHNICAL_ERROR", f"{name} branch failed technically.")

    if not isinstance(issuer, IssuerResult) or not isinstance(risk, RiskResult) or not isinstance(risk.status, RiskStatus):
        return Decision(DecisionState.ERROR, "INVALID_BRANCH_RESULT", "Both valid branch results are required.")
    if issuer is IssuerResult.DECLINED:
        return Decision(DecisionState.DECLINED, "ISSUER_DECLINED", "The synthetic issuer declined the attempt.")
    if risk.status is RiskStatus.VERIFY:
        return Decision(DecisionState.VERIFICATION_REQUIRED, "RISK_REQUIRES_VERIFICATION", "The issuer approved; risk screening requires verification.")
    return Decision(DecisionState.SUCCEEDED, "APPROVED_AND_CLEAR", "The issuer approved and risk screening is clear.")
