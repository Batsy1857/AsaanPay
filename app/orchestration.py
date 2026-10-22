"""Sequential/concurrent orchestration of fictional I/O waits and payment rules."""

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal

from app.contracts import (
    BranchFailure, Decision, DecisionState, IssuerResult, RiskResult, RiskStatus,
)
from app.coordinator import coordinate_decision
from app.simulators import screen_risk, simulate_issuer

# Artificial service waits in milliseconds, never real payment-network timings.
WORKLOAD_PROFILES = {
    "lightweight": (0, 0),
    "balanced": (250, 250),
    "asymmetric": (100, 400),
}


@dataclass(frozen=True)
class OrchestrationResult:
    issuer_result: IssuerResult | BranchFailure
    risk_result: RiskResult | BranchFailure
    outcome: Decision
    timing_trace: dict

    @property
    def decision(self) -> DecisionState | None:
        """Nullable business decision, matching the existing database convention."""
        return None if self.outcome.state is DecisionState.ERROR else self.outcome.state

    @property
    def explanation(self) -> str:
        return self.outcome.reason


async def orchestrate_payment(
    amount_pkr: Decimal,
    card_preset: str,
    billing_country: str,
    execution_mode: str,
    workload_profile: str = "lightweight",
    *,
    branch_timeout_seconds: float = 2.0,
    issuer_fn: Callable[[str], Awaitable[IssuerResult]] = simulate_issuer,
    risk_fn: Callable[[Decimal, str], Awaitable[RiskResult]] = screen_risk,
) -> OrchestrationResult:
    """Run both engines and join with the unchanged pure coordinator.

    Injection and timeout overrides are internal test seams, not HTTP controls.
    Ordinary branch exceptions become technical results; cancellation propagates.
    Injected async functions must cooperate with cancellation and not block the loop.
    """
    if execution_mode not in ("sequential", "concurrent"):
        raise ValueError("execution_mode must be sequential or concurrent")
    if workload_profile not in WORKLOAD_PROFILES:
        raise ValueError("unsupported workload_profile")
    if (isinstance(branch_timeout_seconds, bool)
            or not isinstance(branch_timeout_seconds, (int, float))
            or not math.isfinite(branch_timeout_seconds)
            or branch_timeout_seconds <= 0):
        raise ValueError("branch_timeout_seconds must be finite and positive")

    issuer_delay, risk_delay = WORKLOAD_PROFILES[workload_profile]
    intervals = {}
    branch_traces = {}
    start_ns = time.perf_counter_ns()

    async def run_branch(name, delay_ms, operation, expected_type):
        branch_start = time.perf_counter_ns()
        status = "completed"
        error_type = None
        try:
            # Includes both artificial waiting and engine invocation. The budget
            # starts here, so sequential risk never spends it waiting for issuer.
            async with asyncio.timeout(branch_timeout_seconds):
                await asyncio.sleep(delay_ms / 1000)
                result = await operation()
                if not isinstance(result, expected_type):
                    raise TypeError("Invalid branch result")
                if isinstance(result, RiskResult) and not isinstance(result.status, RiskStatus):
                    raise TypeError("Invalid risk status")
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        except TimeoutError:
            status = "timeout"
            error_type = "TimeoutError"
            result = BranchFailure.TIMEOUT
        except Exception as exc:
            # Only this branch boundary converts exceptions. Do not include raw
            # exception messages (which may contain sensitive diagnostic values).
            status = "technical_error"
            error_type = type(exc).__name__
            result = BranchFailure.TECHNICAL_ERROR
        finally:
            branch_end = time.perf_counter_ns()
            intervals[name] = (branch_start, branch_end)
            branch_traces[name] = {
                "start_ms": (branch_start - start_ns) / 1_000_000,
                "end_ms": (branch_end - start_ns) / 1_000_000,
                "duration_ms": (branch_end - branch_start) / 1_000_000,
                "artificial_delay_ms": delay_ms,
                "status": status,
                "error_type": error_type,
            }
        return result

    if execution_mode == "sequential":
        issuer = await run_branch("issuer", issuer_delay, lambda: issuer_fn(card_preset), IssuerResult)
        risk = await run_branch("risk", risk_delay, lambda: risk_fn(amount_pkr, billing_country), RiskResult)
    else:
        async with asyncio.TaskGroup() as group:
            issuer_task = group.create_task(
                run_branch("issuer", issuer_delay, lambda: issuer_fn(card_preset), IssuerResult)
            )
            risk_task = group.create_task(
                run_branch("risk", risk_delay, lambda: risk_fn(amount_pkr, billing_country), RiskResult)
            )
        # TaskGroup awaits both. result() also propagates a child cancellation,
        # which TaskGroup alone does not raise as an exception from its context.
        issuer, risk = issuer_task.result(), risk_task.result()

    outcome = coordinate_decision(issuer, risk)
    decision_ns = time.perf_counter_ns()
    issuer_start, issuer_end = intervals["issuer"]
    risk_start, risk_end = intervals["risk"]
    overlap_ns = max(0, min(issuer_end, risk_end) - max(issuer_start, risk_start))
    end_ns = time.perf_counter_ns()
    trace = {
        "execution_mode": execution_mode,
        "workload_profile": workload_profile,
        "artificial_delays": True,
        "measurement": "synthetic_orchestration_not_payment_network_latency",
        "branch_timeout_ms": branch_timeout_seconds * 1000,
        "branches": {name: branch_traces[name] for name in ("issuer", "risk")},
        "decision_ms": (decision_ns - start_ns) / 1_000_000,
        "orchestration_ms": (end_ns - start_ns) / 1_000_000,
        "overlap_ms": overlap_ns / 1_000_000,
    }
    return OrchestrationResult(issuer, risk, outcome, trace)
