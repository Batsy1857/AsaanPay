"""Release identity is separate from checkout processing and runtime configuration."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.version import DEV_VERSION, make_metadata, public_version, read_metadata, validate_metadata


class VersionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "release.json"
        self.metadata = make_metadata("1.0.4", "a" * 40, "123", "2")

    def test_valid_release_metadata(self):
        self.path.write_text(json.dumps(self.metadata))
        self.assertEqual(read_metadata(self.path, required=True), self.metadata)
        self.assertEqual(public_version(self.metadata), {"version": "1.0.4", "commit": "a" * 40})

    def test_missing_metadata_has_explicit_dev_fallback(self):
        self.assertIsNone(read_metadata(self.path))
        self.assertEqual(public_version(None), DEV_VERSION)
        with self.assertRaises(ValueError):
            read_metadata(self.path, required=True)

    def test_malformed_metadata_never_falls_back_to_dev(self):
        for content in ("", "{", "null", "[]", "{}", '{"version":"dev","commit":"dev"}'):
            with self.subTest(content=content):
                self.path.write_text(content)
                with self.assertRaises(ValueError):
                    read_metadata(self.path)

    def test_invalid_and_inconsistent_fields_are_rejected(self):
        for field, value in (("version", "01.0.4"), ("version", "sandbox_v1"), ("version", "dev"),
                             ("commit", "abc123"), ("commit", "G" * 40), ("run_id", "0"),
                             ("run_id", 123), ("run_attempt", "-1"), ("release_id", "wrong")):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                validate_metadata({**self.metadata, field: value})
        with self.assertRaises(ValueError):
            validate_metadata({**self.metadata, "extra": "ignored?"})

    def test_endpoint_uses_file_not_environment_or_database(self):
        self.path.write_text(json.dumps(self.metadata))
        with patch("app.version.METADATA_PATH", self.path), patch.dict("os.environ", {
            "APP_VERSION": "wrong", "GIT_COMMIT": "wrong",
        }):
            # No lifespan/database setup: version has no dependency on checkout or DB.
            response = TestClient(app).get("/api/version")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), public_version(self.metadata))

    def test_endpoint_dev_fallback(self):
        with patch("app.version.METADATA_PATH", self.path):
            response = TestClient(app).get("/api/version")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), DEV_VERSION)

    def test_endpoint_invalid_metadata_is_503_without_disclosure(self):
        self.path.write_text('{"private": "invalid metadata"}')
        with patch("app.version.METADATA_PATH", self.path):
            response = TestClient(app).get("/api/version")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "Release metadata is invalid"})
