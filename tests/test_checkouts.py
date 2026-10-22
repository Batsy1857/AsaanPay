"""Validation checks and opt-in HTTP + real PostgreSQL integration tests."""

import asyncio
import hashlib
import json
import os
import unittest
from decimal import Decimal
from uuid import uuid4
from unittest.mock import patch

import asyncpg
import httpx
from fastapi import FastAPI

from app import storage
from app.checkouts import CheckoutInput, get_orchestrator, router
from app.orchestration import orchestrate_payment

PAYMENT = {"amount_pkr": "100.00", "card_preset": "approved", "billing_country": "PK",
           "execution_mode": "concurrent", "workload_profile": "lightweight"}


class ValidationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = FastAPI()
        self.app.include_router(router)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()

    def test_canonical_fingerprint(self):
        a = CheckoutInput.model_validate({**PAYMENT, "amount_pkr": 100, "billing_country": "pk"})
        b = CheckoutInput.model_validate({k: v for k, v in PAYMENT.items() if k != "workload_profile"})
        expected = hashlib.sha256(json.dumps(PAYMENT, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(a.fingerprint(), expected)
        self.assertEqual(a.fingerprint(), b.fingerprint())
        for key, value in (("execution_mode", "sequential"), ("workload_profile", "balanced"),
                           ("amount_pkr", "100.01"), ("card_preset", "declined"), ("billing_country", "US")):
            self.assertNotEqual(a.fingerprint(), CheckoutInput.model_validate({**PAYMENT, key: value}).fingerprint())

    async def test_invalid_bodies_return_422(self):
        invalid = [[], None, {}, {**PAYMENT, "unknown": True}]
        invalid += [{**PAYMENT, "amount_pkr": x} for x in (True, False, 0, -1, "NaN", "Infinity", "1.001", "1000000000000", None)]
        invalid += [{**PAYMENT, "billing_country": x} for x in ("P1", "PAK", " PK", "PK", None)]
        invalid += [{**PAYMENT, key: "invalid"} for key in ("card_preset", "execution_mode", "workload_profile")]
        for payload in invalid:
            with self.subTest(payload=payload):
                response = await self.client.post("/api/checkouts", json=payload, headers={"Idempotency-Key": "test"})
                self.assertEqual(response.status_code, 422)

    async def test_invalid_headers_and_json(self):
        for headers in ({}, {"Idempotency-Key": ""}, {"Idempotency-Key": "with space"},
                        {"Idempotency-Key": "x" * 129}, [("Idempotency-Key", "a"), ("Idempotency-Key", "b")]):
            self.assertEqual((await self.client.post("/api/checkouts", json=PAYMENT, headers=headers)).status_code, 422)
        for body in ('{', '{"amount_pkr": NaN}', b'\xff'):
            self.assertEqual((await self.client.post("/api/checkouts", content=body, headers={"Idempotency-Key": "test"})).status_code, 422)


@unittest.skipUnless(os.getenv("RUN_DB_TESTS") == "1", "Set RUN_DB_TESTS=1 for checkout integration tests")
class CheckoutDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.keys = []
        self.pool = await asyncpg.create_pool(
            host=os.getenv("POSTGRES_HOST", "localhost"), port=int(os.getenv("POSTGRES_PORT", "5432")),
            user=os.environ["POSTGRES_USER"], password=os.environ["POSTGRES_PASSWORD"], database=os.environ["POSTGRES_DB"],
            min_size=1, max_size=1, timeout=3, command_timeout=3,
        )
        # Do not run global startup recovery against a user's running database.
        self.app = FastAPI()
        self.app.include_router(router)
        self.app.state.pool = self.pool
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        try:
            await self.pool.execute("DELETE FROM payment_attempts WHERE idempotency_key=ANY($1::text[])", self.keys)
        finally:
            await self.pool.close()

    def key(self):
        key = "test-checkout-" + str(uuid4())
        self.keys.append(key)
        return key

    async def post(self, key=None, payload=None):
        return await self.client.post("/api/checkouts", json=payload or PAYMENT, headers={"Idempotency-Key": key or self.key()})

    async def test_business_outcomes_modes_and_get(self):
        for preset, amount, country, state in (
            ("approved", "100", "PK", "SUCCEEDED"), ("approved", "50000", "PK", "VERIFICATION_REQUIRED"),
            ("approved", "100", "US", "VERIFICATION_REQUIRED"), ("declined", "50000", "US", "DECLINED"),
        ):
            decisions = []
            for mode in ("sequential", "concurrent"):
                response = await self.post(payload={**PAYMENT, "card_preset": preset, "amount_pkr": amount,
                                                    "billing_country": country, "execution_mode": mode})
                self.assertEqual(response.status_code, 201, response.text)
                body = response.json()
                self.assertEqual(body["state"], state)
                self.assertEqual(body["decision"], state)
                self.assertTrue(body["terminal"])
                self.assertIsNone(body["error_reason"])
                self.assertEqual((await self.client.get(response.headers["Location"])).json(), body)
                self.assertTrue(body["timing_trace"]["artificial_delays"])
                decisions.append((body["decision"], body["issuer_result"], body["risk_result"]))
            self.assertEqual(decisions[0], decisions[1])

    async def test_replay_normalization_and_conflicts(self):
        key = self.key()
        original = await self.post(key)
        with patch("app.checkouts.storage.start_processing", side_effect=AssertionError("must not restart")):
            normalized = {**PAYMENT, "amount_pkr": 100, "billing_country": "pk"}
            normalized.pop("workload_profile")
            replay = await self.post(key, normalized)
            self.assertEqual(replay.status_code, 200)
            self.assertEqual(replay.json(), original.json())
            for field, value in (("amount_pkr", "101"), ("execution_mode", "sequential"), ("workload_profile", "balanced")):
                self.assertEqual((await self.post(key, {**PAYMENT, field: value})).status_code, 409)
        self.assertEqual(await self.pool.fetchval("SELECT count(*) FROM payment_attempts WHERE idempotency_key=$1", key), 1)

    async def test_atomic_claim_race_and_no_connection_held_during_execution(self):
        key = self.key()
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def controlled(**inputs):
            calls.append(True)
            # With max_size=1 this would time out if a transaction/connection was retained.
            row = await asyncio.wait_for(self.pool.fetchrow("SELECT state FROM payment_attempts WHERE idempotency_key=$1", key), 2)
            self.assertEqual(row["state"], "PROCESSING")
            entered.set()
            await release.wait()
            return await orchestrate_payment(**inputs)
        self.app.dependency_overrides[get_orchestrator] = lambda: controlled
        tasks = [asyncio.create_task(self.post(key)) for _ in range(6)]
        try:
            await asyncio.wait_for(entered.wait(), 2)
            # Five competitors can finish while the sole owner is held at a deterministic gate.
            completed = []
            while len(completed) < 5:
                done, _ = await asyncio.wait([task for task in tasks if task not in completed], timeout=2,
                                             return_when=asyncio.FIRST_COMPLETED)
                self.assertTrue(done)
                completed.extend(done)
            for task in completed:
                self.assertEqual(task.result().status_code, 202, task.result().text)
            self.assertEqual((await self.post(key, {**PAYMENT, "amount_pkr": "101"})).status_code, 409)
        finally:
            release.set()
            results = await asyncio.gather(*tasks)
        self.assertEqual(len(calls), 1)
        self.assertEqual(sorted(r.status_code for r in results), [201, 202, 202, 202, 202, 202])
        self.assertEqual(len({r.json()["id"] for r in results}), 1)

    async def test_structured_failure_and_trace_survive_retrieval(self):
        captured = []
        async def fail(*args):
            raise OSError("private diagnostic")
        async def controlled(**inputs):
            result = await orchestrate_payment(**inputs, risk_fn=fail)
            captured.append(result)
            return result
        self.app.dependency_overrides[get_orchestrator] = lambda: controlled
        key = self.key()
        response = await self.post(key)
        body = response.json()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(body["state"], "ERROR")
        self.assertIsNone(body["decision"])
        self.assertIsNone(body["risk_result"])
        self.assertTrue(body["error_reason"])
        self.assertNotIn("private diagnostic", response.text)
        self.assertEqual(body["timing_trace"], captured[0].timing_trace)
        self.assertEqual((await self.client.get(response.headers["Location"])).json(), body)
        self.assertEqual((await self.post(key)).status_code, 200)
        self.assertEqual(len(captured), 1)

    async def test_unexpected_exception_is_sanitized(self):
        async def fail(**inputs):
            raise RuntimeError("password=private SQL statement")
        self.app.dependency_overrides[get_orchestrator] = lambda: fail
        response = await self.post()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["state"], "ERROR")
        self.assertIsNone(response.json()["decision"])
        self.assertNotIn("private", response.text)

    async def test_persistence_failure_never_reports_success(self):
        key = self.key()
        with patch("app.checkouts.storage.finish", side_effect=storage.StorageError("private")):
            response = await self.post(key)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private", response.text)
        self.assertEqual((await self.post(key)).status_code, 202)

    async def test_cancellation_persists_error_without_orphaned_work(self):
        key = self.key()
        entered = asyncio.Event()
        cleaned = []
        async def controlled(**inputs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.append(True)
        self.app.dependency_overrides[get_orchestrator] = lambda: controlled
        task = asyncio.create_task(self.post(key))
        try:
            await asyncio.wait_for(entered.wait(), 2)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(cleaned, [True])
        replay = await self.post(key)
        self.assertEqual(replay.status_code, 200)
        self.assertIn("REQUEST_CANCELLED", replay.json()["error_reason"])

    async def test_guards_preserve_terminal_and_request_identity(self):
        key = self.key()
        created = (await self.post(key)).json()
        row = await self.pool.fetchrow("SELECT * FROM payment_attempts WHERE idempotency_key=$1", key)
        with self.assertRaises(storage.StorageError):
            await storage.start_processing(self.pool, row["id"])
        self.assertIsNone(await storage.fail(self.pool, row["id"], "should not overwrite"))
        after = await storage.get_attempt(self.pool, row["id"])
        self.assertEqual(row, after)
        self.assertEqual((await self.client.get('/api/checkouts/' + created['id'])).json(), created)

    async def test_unknown_get_and_database_outage(self):
        self.assertEqual((await self.client.get('/api/checkouts/' + str(uuid4()))).status_code, 404)
        with patch("app.checkouts.storage.get_attempt", side_effect=storage.StorageError("private")):
            response = await self.client.get('/api/checkouts/' + str(uuid4()))
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private", response.text)

    async def test_json_numbers_are_parsed_exactly_without_float_rounding(self):
        key = self.key()
        raw = json.dumps(PAYMENT).replace('"100.00"', '999999999999.99')
        response = await self.client.post('/api/checkouts', content=raw, headers={"Idempotency-Key": key})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["request"]["amount_pkr"], "999999999999.99")
        raw = json.dumps(PAYMENT).replace('"100.00"', '999999999999.991')
        self.assertEqual((await self.client.post('/api/checkouts', content=raw, headers={"Idempotency-Key": self.key()})).status_code, 422)
