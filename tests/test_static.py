"""Static serving must coexist with the existing API and lifecycle."""

import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.main import app


class StaticTests(unittest.TestCase):
    def setUp(self):
        self.pool = AsyncMock()
        self.pool.fetchval.return_value = 1
        self.env = patch.dict("os.environ", {
            "POSTGRES_USER": "test", "POSTGRES_PASSWORD": "test", "POSTGRES_DB": "test",
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_checkout_page_and_assets(self):
        with patch("app.main.asyncpg.create_pool", AsyncMock(return_value=self.pool)):
            with TestClient(app) as client:
                response = client.get("/")
                self.assertEqual(response.status_code, 200)
                self.assertIn("text/html", response.headers["content-type"])
                self.assertIn('id="checkout-form"', response.text)
                for filename, content_type in (("styles.css", "text/css"), ("core.js", "javascript"), ("app.js", "javascript"), ("version.js", "javascript")):
                    response = client.get("/static/" + filename)
                    self.assertEqual(response.status_code, 200)
                    self.assertIn(content_type, response.headers["content-type"])
                    self.assertTrue(response.content)

    def test_api_routes_are_not_shadowed(self):
        with patch("app.main.asyncpg.create_pool", AsyncMock(return_value=self.pool)):
            with TestClient(app) as client:
                self.assertEqual(client.get("/api/health").json(), {"status": "ok", "database": "connected"})
                self.assertEqual(client.post("/api/checkouts", json={}).status_code, 422)
                self.assertEqual(client.get("/api/checkouts/invalid").status_code, 422)
                self.assertEqual(client.get("/api/unknown").status_code, 404)

    def test_missing_assets_and_traversal_are_not_served(self):
        with patch("app.main.asyncpg.create_pool", AsyncMock(return_value=self.pool)):
            with TestClient(app) as client:
                for path in ("/static/missing.js", "/static/%2e%2e/main.py", "/static/%2e%2e/%2e%2e/.env"):
                    self.assertEqual(client.get(path).status_code, 404)
