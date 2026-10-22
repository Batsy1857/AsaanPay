import asyncio
import gc
import json
import unittest
from decimal import Decimal
from itertools import product

from app.contracts import BranchFailure, DecisionState, IssuerResult
from app.orchestration import orchestrate_payment
from app.simulators import screen_risk, simulate_issuer


class OrchestrationTests(unittest.IsolatedAsyncioTestCase):
    async def run_payment(self, mode, profile="lightweight", **kwargs):
        return await orchestrate_payment(Decimal("100"), "approved", "PK", mode, profile, **kwargs)

    def assert_trace(self, result):
        trace = result.timing_trace
        json.dumps(trace, allow_nan=False)
        self.assertTrue(trace["artificial_delays"])
        issuer, risk = (trace["branches"][name] for name in ("issuer", "risk"))
        for branch in (issuer, risk):
            self.assertGreaterEqual(branch["start_ms"], 0)
            self.assertGreaterEqual(branch["end_ms"], branch["start_ms"])
            self.assertAlmostEqual(branch["duration_ms"], branch["end_ms"] - branch["start_ms"], places=6)
            self.assertGreaterEqual(trace["decision_ms"], branch["end_ms"])
        self.assertGreaterEqual(trace["orchestration_ms"], trace["decision_ms"])
        overlap = max(0, min(issuer["end_ms"], risk["end_ms"]) - max(issuer["start_ms"], risk["start_ms"]))
        self.assertAlmostEqual(trace["overlap_ms"], overlap, places=6)
        if trace["execution_mode"] == "sequential":
            self.assertGreaterEqual(risk["start_ms"], issuer["end_ms"])
            self.assertEqual(trace["overlap_ms"], 0)

    async def test_matrix_and_multiple_inputs_equivalent_in_lightweight_modes(self):
        for preset in ("approved", "declined"):
            for amount, country, verify in (
                ("49999.99", "PK", False), ("50000", "PK", True),
                ("50000.01", "PK", True), ("10", "US", True), ("50000", "US", True),
            ):
                with self.subTest(preset=preset, amount=amount, country=country):
                    results = [await orchestrate_payment(Decimal(amount), preset, country, mode)
                               for mode in ("sequential", "concurrent")]
                    expected = (DecisionState.DECLINED if preset == "declined" else
                                DecisionState.VERIFICATION_REQUIRED if verify else DecisionState.SUCCEEDED)
                    self.assertEqual(results[0].outcome, results[1].outcome)
                    self.assertEqual(results[0].issuer_result, results[1].issuer_result)
                    self.assertEqual(results[0].risk_result, results[1].risk_result)
                    for result in results:
                        self.assertIs(result.decision, expected)
                        self.assert_trace(result)

    async def test_balanced_real_overlap_and_sequential_order(self):
        for mode in ("sequential", "concurrent"):
            result = await self.run_payment(mode, "balanced")
            self.assert_trace(result)
            branches = result.timing_trace["branches"]
            for branch in branches.values():
                self.assertEqual(branch["artificial_delay_ms"], 250)
                self.assertGreaterEqual(branch["duration_ms"], 240)
            if mode == "concurrent":
                self.assertGreater(result.timing_trace["overlap_ms"], 0)
                self.assertLess(branches["risk"]["start_ms"], branches["issuer"]["end_ms"])
            self.assertIs(result.decision, DecisionState.SUCCEEDED)

    async def test_asymmetric_critical_path(self):
        for mode in ("sequential", "concurrent"):
            result = await self.run_payment(mode, "asymmetric")
            self.assert_trace(result)
            trace = result.timing_trace
            issuer, risk = trace["branches"]["issuer"], trace["branches"]["risk"]
            self.assertEqual(issuer["artificial_delay_ms"], 100)
            self.assertEqual(risk["artificial_delay_ms"], 400)
            self.assertGreaterEqual(risk["duration_ms"], 390)
            if mode == "concurrent":
                self.assertLess(issuer["end_ms"], risk["end_ms"])
                self.assertGreater(trace["overlap_ms"], 0)
                # The measured union is the critical path, not the sum of waits.
                union = max(issuer["end_ms"], risk["end_ms"]) - min(issuer["start_ms"], risk["start_ms"])
                self.assertAlmostEqual(union, issuer["duration_ms"] + risk["duration_ms"] - trace["overlap_ms"], places=6)

    async def test_completion_order_does_not_change_decision(self):
        for first, preset, (amount, country) in product(
            ("issuer", "risk"), ("approved", "declined"),
            (("100", "PK"), ("50000", "PK"), ("100", "US"), ("50000", "US")),
        ):
            gate = asyncio.Event()
            order = []
            async def issuer(preset):
                if first == "risk":
                    await gate.wait()
                result = await simulate_issuer(preset)
                order.append("issuer")
                gate.set()
                return result
            async def risk(amount, country):
                if first == "issuer":
                    await gate.wait()
                result = await screen_risk(amount, country)
                order.append("risk")
                gate.set()
                return result
            result = await orchestrate_payment(Decimal(amount), preset, country, "concurrent", issuer_fn=issuer, risk_fn=risk)
            expected = await orchestrate_payment(Decimal(amount), preset, country, "sequential")
            self.assertEqual(order[0], first)
            self.assertEqual(result.outcome, expected.outcome)
            self.assertEqual(result.risk_result, expected.risk_result)
            self.assert_trace(result)

    async def test_failures_in_either_branch_and_both_modes(self):
        async def fail(*args):
            raise RuntimeError("private exception detail")
        for mode in ("sequential", "concurrent"):
            for injection, branch in (({"issuer_fn": fail}, "issuer"), ({"risk_fn": fail}, "risk")):
                result = await self.run_payment(mode, **injection)
                self.assertIs(result.outcome.state, DecisionState.ERROR)
                self.assertIsNone(result.decision)
                self.assertIn(branch.title(), result.explanation)
                self.assertEqual(result.timing_trace["branches"][branch]["status"], "technical_error")
                self.assertNotIn("private exception detail", json.dumps(result.timing_trace))
                self.assert_trace(result)

    async def test_business_decline_does_not_skip_failing_risk(self):
        async def fail(*args):
            raise OSError("unavailable")
        for mode in ("sequential", "concurrent"):
            result = await orchestrate_payment(Decimal("100"), "declined", "PK", mode, risk_fn=fail)
            self.assertIs(result.issuer_result, IssuerResult.DECLINED)
            self.assertIs(result.risk_result, BranchFailure.TECHNICAL_ERROR)
            self.assertIsNone(result.decision)
            self.assertIs(result.outcome.state, DecisionState.ERROR)

    async def test_timeout_includes_artificial_wait(self):
        called = []
        async def issuer(preset):
            called.append("issuer")
            return await simulate_issuer(preset)
        for mode in ("sequential", "concurrent"):
            result = await self.run_payment(mode, "balanced", branch_timeout_seconds=0.02, issuer_fn=issuer)
            self.assertIs(result.issuer_result, BranchFailure.TIMEOUT)
            self.assertIs(result.risk_result, BranchFailure.TIMEOUT)
            self.assertIsNone(result.decision)
            self.assert_trace(result)
        self.assertEqual(called, [])

    async def test_timeout_in_engine_awaits_cleanup(self):
        for mode in ("sequential", "concurrent"):
            for branch in ("issuer", "risk"):
                cleaned = []
                async def wait_forever(*args):
                    try:
                        await asyncio.Event().wait()
                    finally:
                        cleaned.append(True)
                result = await self.run_payment(mode, branch_timeout_seconds=0.02, **{branch + "_fn": wait_forever})
                self.assertEqual(cleaned, [True])
                self.assertIs(result.outcome.state, DecisionState.ERROR)
                self.assertIsNone(result.decision)
                self.assertEqual(result.timing_trace["branches"][branch]["status"], "timeout")

    async def test_sequential_risk_has_its_own_timeout_budget(self):
        result = await self.run_payment("sequential", "balanced", branch_timeout_seconds=0.45)
        self.assertIs(result.decision, DecisionState.SUCCEEDED)
        self.assert_trace(result)

    async def test_failure_paths_join_tasks_and_observe_exceptions(self):
        loop = asyncio.get_running_loop()
        previous = loop.get_exception_handler()
        unobserved = []
        loop.set_exception_handler(lambda loop, context: unobserved.append(context))
        baseline = asyncio.all_tasks()
        finished = []
        async def fail(*args):
            try:
                await asyncio.sleep(0)
                raise ValueError("synthetic failure")
            finally:
                finished.append(True)
        try:
            for mode in ("sequential", "concurrent"):
                result = await self.run_payment(mode, issuer_fn=fail, risk_fn=fail)
                self.assertIsNone(result.decision)
                self.assertIn("Issuer", result.explanation)
            gc.collect()
            await asyncio.sleep(0)
            self.assertEqual(len(finished), 4)
            self.assertEqual(asyncio.all_tasks() - baseline, set())
            self.assertEqual(unobserved, [])
        finally:
            loop.set_exception_handler(previous)

    async def test_parent_cancellation_propagates_and_cleans_up(self):
        for mode in ("sequential", "concurrent"):
            started, cleaned = [], []
            ready = asyncio.Event()
            async def wait_forever(*args):
                started.append(True)
                if len(started) == (2 if mode == "concurrent" else 1):
                    ready.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaned.append(True)
            baseline = asyncio.all_tasks()
            task = asyncio.create_task(self.run_payment(mode, issuer_fn=wait_forever, risk_fn=wait_forever))
            try:
                await asyncio.wait_for(ready.wait(), timeout=2)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertEqual(len(cleaned), len(started))
            self.assertEqual(asyncio.all_tasks() - baseline, set())

    async def test_branch_cancellation_is_not_a_technical_result(self):
        async def cancel(*args):
            raise asyncio.CancelledError
        for mode in ("sequential", "concurrent"):
            for branch in ("issuer", "risk"):
                with self.assertRaises(asyncio.CancelledError):
                    await self.run_payment(mode, **{branch + "_fn": cancel})

    async def test_invalid_branch_results_become_technical_errors(self):
        async def invalid(*args):
            return "APPROVED"
        for mode in ("sequential", "concurrent"):
            result = await self.run_payment(mode, issuer_fn=invalid)
            self.assertIs(result.issuer_result, BranchFailure.TECHNICAL_ERROR)
            self.assertIsNone(result.decision)

    async def test_invalid_controls_are_rejected(self):
        for options in ({"execution_mode": "other"}, {"workload_profile": "other"},
                        *({"branch_timeout_seconds": x} for x in (0, -1, float("nan"), float("inf"), True))):
            args = dict(amount_pkr=Decimal("100"), card_preset="approved", billing_country="PK", execution_mode="sequential")
            args.update(options)
            with self.assertRaises(ValueError):
                await orchestrate_payment(**args)
