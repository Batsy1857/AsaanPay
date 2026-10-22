"""Opt-in real PostgreSQL checks; all inserted fixtures are rolled back."""
import os
import unittest
import uuid

import asyncpg


@unittest.skipUnless(os.getenv("RUN_DB_TESTS") == "1", "Set RUN_DB_TESTS=1 for live PostgreSQL checks")
class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_connectivity_schema_and_constraints(self):
        connection = await asyncpg.connect(
            host=os.getenv("POSTGRES_HOST", "localhost"),
            port=int(os.getenv("POSTGRES_PORT", "5432")),
            user=os.environ["POSTGRES_USER"],
            password=os.environ["POSTGRES_PASSWORD"],
            database=os.environ["POSTGRES_DB"], timeout=3,
        )
        try:
            self.assertEqual(await connection.fetchval("SELECT 1"), 1)
            transaction = connection.transaction()
            await transaction.start()
            try:
                query = """INSERT INTO payment_attempts
                    (idempotency_key, request_fingerprint, amount_pkr, card_preset,
                     billing_country, execution_mode)
                    VALUES ($1, $2, $3, 'approved', 'PK', 'sequential')
                    RETURNING id, state"""
                key = str(uuid.uuid4())
                row = await connection.fetchrow(query, key, "a" * 64, 100)
                self.assertIsInstance(row["id"], uuid.UUID)
                self.assertEqual(row["state"], "CREATED")
                with self.assertRaises(asyncpg.UniqueViolationError):
                    async with connection.transaction():
                        await connection.execute(query, key, "a" * 64, 100)
                with self.assertRaises(asyncpg.CheckViolationError):
                    async with connection.transaction():
                        await connection.execute(query, str(uuid.uuid4()), "a" * 64, -1)
            finally:
                await transaction.rollback()
        finally:
            await connection.close()
