"""Integrity and security-policy negative tests; no Docker/network required."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from app.version import make_metadata
from scripts.release import check_checksums, digest, validate_image, validate_manifest
from scripts.trivy_policy import evaluate

IMAGE_ID = "sha256:" + "b" * 64


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.metadata = make_metadata("1.0.4", "a" * 40, "123", "1")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        (self.directory / "image.tar.gz").write_bytes(b"synthetic archive for checksum validation")
        self.manifest = {**self.metadata, "schema_version": 1, "platform": "linux/amd64",
                         "image": "asaanpay:" + self.metadata["release_id"], "image_id": IMAGE_ID,
                         "archive": "image.tar.gz", "archive_sha256": digest(self.directory / "image.tar.gz"),
                         "local_validation": False, "source_dirty": False}
        self.write_package()

    def write_package(self):
        (self.directory / "manifest.json").write_text(json.dumps(self.manifest))
        (self.directory / "SHA256SUMS").write_text("".join(
            f"{digest(self.directory / name)}  {name}\n" for name in ("image.tar.gz", "manifest.json")))

    def test_package_checksums_and_metadata(self):
        self.assertEqual(check_checksums(self.directory), self.manifest)

    def test_corrupt_archive_or_manifest_rejected(self):
        for filename in ("image.tar.gz", "manifest.json"):
            path = self.directory / filename
            original = path.read_bytes()
            path.write_bytes(original + b"corruption")
            with self.assertRaises(ValueError):
                check_checksums(self.directory)
            path.write_bytes(original)

    def test_missing_duplicate_and_unsafe_checksum_entries_rejected(self):
        for value in ("", f"{'a' * 64}  ../outside\n", f"{'a' * 64}  image.tar.gz\n" * 2):
            (self.directory / "SHA256SUMS").write_text(value)
            with self.assertRaises(ValueError):
                check_checksums(self.directory)

    def test_consistent_checksums_cannot_hide_inconsistent_manifest(self):
        self.manifest["archive_sha256"] = "0" * 64
        self.write_package()
        with self.assertRaises(ValueError):
            check_checksums(self.directory)

    def test_unexpected_files_not_packaged(self):
        (self.directory / ".env").write_text("synthetic fixture")
        with self.assertRaises(ValueError):
            check_checksums(self.directory)

    def test_wrong_manifest_architecture_tag_and_dirty_provenance_rejected(self):
        for field, value in (("platform", "linux/arm64"), ("image", "asaanpay:latest"),
                             ("image_id", "unknown"), ("source_dirty", True), ("commit", "c" * 40)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_manifest({**self.manifest, field: value})

    def test_image_id_platform_and_labels_must_match(self):
        labels = {"org.opencontainers.image.version": self.metadata["version"],
                  "org.opencontainers.image.revision": self.metadata["commit"],
                  "io.asaanpay.release-id": self.metadata["release_id"],
                  "io.asaanpay.run-id": "123", "io.asaanpay.run-attempt": "1"}
        image = {"Id": IMAGE_ID, "Os": "linux", "Architecture": "amd64",
                 "Config": {"Labels": labels, "User": "10001:10001"}}
        validate_image(image, self.metadata, IMAGE_ID)
        for field, value in (("Id", "sha256:" + "c" * 64), ("Architecture", "arm64")):
            with self.assertRaises(ValueError):
                validate_image({**image, field: value}, self.metadata, IMAGE_ID)
        for label in labels:
            modified = deepcopy(image)
            modified["Config"]["Labels"][label] = "wrong"
            with self.assertRaises(ValueError):
                validate_image(modified, self.metadata, IMAGE_ID)


class TrivyPolicyTests(unittest.TestCase):
    def setUp(self):
        self.report = {"SchemaVersion": 2, "ArtifactType": "container_image",
                       "Metadata": {"ImageID": IMAGE_ID, "OS": {"Family": "debian", "EOSL": False}},
                       "Results": [
                           {"Class": "os-pkgs", "Type": "debian", "Target": "debian", "Packages": [{"Name": "libc6"}]},
                           {"Class": "lang-pkgs", "Type": "python-pkg", "Target": "Python",
                            "Packages": [{"Name": name} for name in ("fastapi", "uvicorn", "asyncpg", "httpx")]},
                       ]}

    def finding(self, severity, fixed):
        return {"Severity": severity, "FixedVersion": fixed, "VulnerabilityID": "CVE-fixture",
                "PkgName": "fixture", "InstalledVersion": "1"}

    def test_complete_clean_scan(self):
        self.assertEqual(evaluate(self.report, IMAGE_ID)[0], 0)

    def test_fixable_high_critical_block_for_both_package_types(self):
        for item in self.report["Results"]:
            for severity in ("HIGH", "CRITICAL"):
                item["Vulnerabilities"] = [self.finding(severity, "2")]
                self.assertGreater(evaluate(self.report, IMAGE_ID)[0], 0)
            del item["Vulnerabilities"]

    def test_unfixed_are_reported_and_medium_does_not_block(self):
        self.report["Results"][0]["Vulnerabilities"] = [self.finding("HIGH", ""), self.finding("CRITICAL", ""),
                                                       self.finding("MEDIUM", "2")]
        blocking, summary = evaluate(self.report, IMAGE_ID)
        self.assertEqual(blocking, 0)
        self.assertIn("REVIEW CRITICAL", summary)
        self.assertIn("HIGH | 0 | 1", summary)

    def test_missing_os_python_inventory_and_wrong_image_fail(self):
        variants = []
        for index in (0, 1):
            missing = deepcopy(self.report)
            missing["Results"].pop(index)
            variants.append(missing)
        wrong = deepcopy(self.report)
        wrong["Metadata"]["ImageID"] = "sha256:" + "c" * 64
        variants.append(wrong)
        unsupported = deepcopy(self.report)
        unsupported["Metadata"]["OS"]["EOSL"] = True
        variants.append(unsupported)
        variants.append({})
        for report in variants:
            with self.assertRaises((ValueError, KeyError)):
                evaluate(report, IMAGE_ID)
