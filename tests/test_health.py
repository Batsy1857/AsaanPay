import unittest
from unittest.mock import AsyncMock, patch

import asyncpg
from fastapi.testclient import TestClient

from app.main import app


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.pool = AsyncMock()
        self.pool.fetchval.return_value = 1
        self.env = patch.dict("os.environ", {
            "POSTGRES_USER": "test", "POSTGRES_PASSWORD": "test", "POSTGRES_DB": "test"
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_health_and_pool_lifecycle(self):
        with patch("app.main.asyncpg.create_pool", AsyncMock(return_value=self.pool)) as create:
            with TestClient(app) as client:
                response = client.get("/api/health")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {"status": "ok", "database": "connected"})
                self.pool.fetchval.assert_awaited_once_with("SELECT 1")
                self.pool.close.assert_not_awaited()
            create.assert_awaited_once()
            self.pool.close.assert_awaited_once()
            self.pool.execute.assert_awaited_once()
            self.assertIn("WHERE state IN", self.pool.execute.call_args.args[0])

    def test_database_outage_and_recovery(self):
        with patch("app.main.asyncpg.create_pool", AsyncMock(return_value=self.pool)):
            with TestClient(app) as client:
                for error in (OSError("private connection detail"), TimeoutError()):
                    self.pool.fetchval.side_effect = error
                    response = client.get("/api/health")
                    self.assertEqual(response.status_code, 503)
                    self.assertEqual(response.json(), {"status": "unhealthy", "database": "unavailable"})
                self.pool.fetchval.side_effect = None
                self.assertEqual(client.get("/api/health").status_code, 200)

    def test_startup_failure_is_clear(self):
        with patch("app.main.asyncpg.create_pool", AsyncMock(side_effect=OSError("secret"))):
            with self.assertRaisesRegex(RuntimeError, "PostgreSQL startup failed"):
                with TestClient(app):
                    self.fail("Startup must not succeed")

    def test_startup_diagnostics_do_not_expose_credentials(self):
        secret = "postgresql://user:private-password@private-host/private-db"
        with patch.dict("os.environ", {
            "POSTGRES_HOST": "db", "POSTGRES_PORT": "5432",
            "POSTGRES_PASSWORD": "private-password", "DATABASE_URL": secret,
        }):
            for error in (asyncpg.InvalidPasswordError(secret), TimeoutError(secret)):
                with self.subTest(error=type(error).__name__):
                    with patch("app.main.asyncpg.create_pool", AsyncMock(side_effect=error)):
                        with self.assertLogs("app.main", level="ERROR") as logs:
                            with self.assertRaises(RuntimeError) as raised:
                                with TestClient(app):
                                    self.fail("Startup must not succeed")
                    output = " ".join(logs.output)
                    self.assertIn("exception=" + type(error).__name__, output)
                    self.assertIn("sqlstate=" + str(getattr(error, "sqlstate", None)), output)
                    self.assertIn("host=db port=5432", output)
                    self.assertIn("DATABASE_URL_present=True", output)
                    self.assertNotIn("private-", output + str(raised.exception))
                    self.assertTrue(raised.exception.__suppress_context__)
                    self.assertIsNone(logs.records[0].exc_info)

    def test_invalid_port_diagnostic_does_not_echo_value(self):
        with patch.dict("os.environ", {"POSTGRES_PORT": "postgresql://secret-value"}):
            with self.assertLogs("app.main", level="ERROR") as logs:
                with self.assertRaises(RuntimeError):
                    with TestClient(app):
                        self.fail("Startup must not succeed")
        self.assertIn("exception=ValueError", logs.output[0])
        self.assertNotIn("secret-value", logs.output[0])

    def test_recovery_failure_closes_pool_and_stops_startup(self):
        self.pool.execute.side_effect = asyncpg.UndefinedTableError("private SQL")
        with patch("app.main.asyncpg.create_pool", AsyncMock(return_value=self.pool)):
            with self.assertRaisesRegex(RuntimeError, "startup recovery failed") as raised:
                with TestClient(app):
                    self.fail("Recovery failure must prevent startup")
        self.pool.close.assert_awaited_once()
        self.assertNotIn("private", str(raised.exception))

    def test_checkout_routes_validate_inputs(self):
        with patch("app.main.asyncpg.create_pool", AsyncMock(return_value=self.pool)):
            with TestClient(app) as client:
                self.assertEqual(client.post("/api/checkouts", json={}).status_code, 422)
                self.assertEqual(client.get("/api/checkouts/example").status_code, 422)
