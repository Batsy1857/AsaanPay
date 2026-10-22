"""Behavioral tests for the fictional engines and pure coordinator."""

import unittest
from dataclasses import FrozenInstanceError
from decimal import Decimal, localcontext

from app.contracts import (
    BranchFailure, DecisionState, IssuerResult, RiskReason, RiskResult, RiskStatus,
)
from app.coordinator import coordinate_decision
from app.simulators import screen_risk, simulate_issuer


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_supported_presets(self):
        self.assertIs(await simulate_issuer("approved"), IssuerResult.APPROVED)
        self.assertIs(await simulate_issuer("declined"), IssuerResult.DECLINED)

    async def test_unsupported_presets(self):
        for preset in ("unknown", "APPROVED", "", None, 123, ["approved"]):
            with self.subTest(preset=preset), self.assertRaises(ValueError):
                await simulate_issuer(preset)

    async def test_threshold_and_country_combinations(self):
        for amount in ("49999.99", "50000", "50000.01"):
            for country in ("PK", "US"):
                with self.subTest(amount=amount, country=country):
                    result = await screen_risk(Decimal(amount), country)
                    expected = {
                        ("49999.99", "PK"): (),
                        ("49999.99", "US"): (RiskReason.CROSS_BORDER,),
                        ("50000", "PK"): (RiskReason.HIGH_VALUE,),
                        ("50000", "US"): (RiskReason.HIGH_VALUE, RiskReason.CROSS_BORDER),
                        ("50000.01", "PK"): (RiskReason.HIGH_VALUE,),
                        ("50000.01", "US"): (RiskReason.HIGH_VALUE, RiskReason.CROSS_BORDER),
                    }[(amount, country)]
                    self.assertEqual(result.reasons, expected)
                    self.assertIs(result.status, RiskStatus.VERIFY if expected else RiskStatus.CLEAR)
                    if RiskReason.HIGH_VALUE in expected or not expected:
                        self.assertIn(amount, result.explanation)
                    if RiskReason.CROSS_BORDER in expected:
                        self.assertIn("US", result.explanation)
                        self.assertIn("PK", result.explanation)
                    self.assertTrue(result.explanation)

    async def test_country_normalization(self):
        self.assertEqual(await screen_risk(Decimal("10"), "pk"), await screen_risk(Decimal("10"), "PK"))
        self.assertEqual(await screen_risk(Decimal("10"), "us"), await screen_risk(Decimal("10"), "US"))

    async def test_invalid_inputs_are_rejected(self):
        for amount in (Decimal("0"), Decimal("-1"), Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity"), Decimal("1000000000000"), Decimal("1.001"), Decimal("0.00001")):
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                await screen_risk(amount, "PK")
        for amount in (50000, 49999.99, "50000", True, None):
            with self.subTest(amount=amount), self.assertRaises(TypeError):
                await screen_risk(amount, "PK")
        for country in ("", "PAK", "P1", " PK", "PK", None):
            with self.subTest(country=country), self.assertRaises(ValueError):
                await screen_risk(Decimal("1"), country)

    async def test_decimal_precision_is_not_rounded(self):
        with localcontext() as context:
            context.prec = 3
            self.assertIs((await screen_risk(Decimal("49999.99"), "PK")).status, RiskStatus.CLEAR)
            self.assertIs((await screen_risk(Decimal("50000.000"), "PK")).status, RiskStatus.VERIFY)
            self.assertIs((await screen_risk(Decimal("999999999999.99"), "PK")).status, RiskStatus.VERIFY)

    async def test_deterministic_repeated_results(self):
        for preset in ("approved", "declined"):
            for amount, country in (("10", "PK"), ("50000", "PK"), ("10", "US"), ("50000", "US")):
                issuer = await simulate_issuer(preset)
                risk = await screen_risk(Decimal(amount), country)
                decision = coordinate_decision(issuer, risk)
                for _ in range(5):
                    self.assertEqual(await simulate_issuer(preset), issuer)
                    self.assertEqual(await screen_risk(Decimal(amount), country), risk)
                    self.assertEqual(coordinate_decision(issuer, risk), decision)


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.clear = RiskResult(RiskStatus.CLEAR, (), "Domestic, below threshold.")
        self.verify = RiskResult(RiskStatus.VERIFY, (RiskReason.HIGH_VALUE,), "High value.")

    def test_complete_matrix(self):
        cases = (
            (IssuerResult.APPROVED, self.clear, DecisionState.SUCCEEDED, "APPROVED_AND_CLEAR"),
            (IssuerResult.APPROVED, self.verify, DecisionState.VERIFICATION_REQUIRED, "RISK_REQUIRES_VERIFICATION"),
            (IssuerResult.DECLINED, self.clear, DecisionState.DECLINED, "ISSUER_DECLINED"),
            (IssuerResult.DECLINED, self.verify, DecisionState.DECLINED, "ISSUER_DECLINED"),
        )
        for issuer, risk, state, reason_code in cases:
            with self.subTest(issuer=issuer, risk=risk.status):
                result = coordinate_decision(issuer, risk)
                self.assertIs(result.state, state)
                self.assertEqual(result.reason_code, reason_code)
                self.assertTrue(result.reason)

    def test_technical_errors_and_timeouts_override_business_results(self):
        for failure in BranchFailure:
            for issuer, risk, branch in (
                (failure, self.clear, "Issuer"), (failure, self.verify, "Issuer"),
                (IssuerResult.APPROVED, failure, "Risk"), (IssuerResult.DECLINED, failure, "Risk"),
                (None, failure, "Risk"), (failure, None, "Issuer"),
            ):
                with self.subTest(issuer=issuer, risk=risk):
                    decision = coordinate_decision(issuer, risk)
                    self.assertIs(decision.state, DecisionState.ERROR)
                    self.assertIn(branch, decision.reason)
                    self.assertEqual(decision.reason_code, "BRANCH_TIMEOUT" if failure is BranchFailure.TIMEOUT else "BRANCH_TECHNICAL_ERROR")

    def test_two_failures_have_stable_issuer_priority(self):
        result = coordinate_decision(BranchFailure.TECHNICAL_ERROR, BranchFailure.TIMEOUT)
        self.assertEqual(result.reason_code, "BRANCH_TECHNICAL_ERROR")
        self.assertIn("Issuer", result.reason)

    def test_missing_or_invalid_results_fail_closed(self):
        for issuer, risk in ((None, self.clear), (IssuerResult.DECLINED, None), ("APPROVED", self.clear), (IssuerResult.APPROVED, "CLEAR"), (IssuerResult.APPROVED, RiskResult("CLEAR", (), "invalid"))):
            with self.subTest(issuer=issuer, risk=risk):
                result = coordinate_decision(issuer, risk)
                self.assertIs(result.state, DecisionState.ERROR)
                self.assertEqual(result.reason_code, "INVALID_BRANCH_RESULT")

    def test_results_are_immutable(self):
        decision = coordinate_decision(IssuerResult.APPROVED, self.clear)
        with self.assertRaises(FrozenInstanceError):
            decision.reason = "changed"
        with self.assertRaises(FrozenInstanceError):
            self.clear.status = RiskStatus.VERIFY
        self.assertEqual(coordinate_decision(IssuerResult.APPROVED, self.clear), decision)
