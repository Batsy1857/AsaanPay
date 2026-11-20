"""Integrity and security-policy negative tests; no Docker/network required."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
import subprocess
import sys

from app.version import make_metadata
from scripts.release import check_checksums, digest, validate_image, validate_manifest
from scripts.trivy_policy import evaluate, read_triage

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


class TrivyFixture:
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


class TrivyPolicyTests(TrivyFixture, unittest.TestCase):
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


class TrivySummaryTests(TrivyFixture, unittest.TestCase):
    """Different CVE/source cardinalities prevent counts becoming baseline constants."""

    def setUp(self):
        super().setUp()
        self.report['Metadata']['OS']['Name'] = '13.7'
        self.report['Results'][0]['Packages'] = [
            {'ID': 'binary-a@1', 'Name': 'binary-a', 'SrcName': 'source-one'},
            {'ID': 'binary-b@1', 'Name': 'binary-b', 'SrcName': 'source-one'},
        ]
        self.report['Results'][0]['Vulnerabilities'] = [
            self.high('CVE-2026-10001', 'binary-a'), self.high('CVE-2026-10001', 'binary-b'),
            self.high('CVE-2026-10002', 'binary-a'),
        ]
        self.triage = {'schema_version': 1, 'distribution': 'debian', 'release': '13',
                       'reviewed': '2026-11-20', 'review_due': '2026-12-04',
                       'entries': {'CVE-2026-10001': 'source-one'}}

    def high(self, cve, package):
        return {**self.finding('HIGH', ''), 'VulnerabilityID': cve, 'PkgName': package,
                'PkgID': package + '@1', 'Status': 'affected'}

    def index_text(self, data=None):
        return '<!-- trivy-triage:start -->\n```json\n' + json.dumps(
            self.triage if data is None else data) + '\n```\n<!-- trivy-triage:end -->\n'

    def test_grouping_counts_binaries_cves_and_sources_independently(self):
        blocking, summary = evaluate(self.report, IMAGE_ID, self.triage)
        self.assertEqual(blocking, 0)
        self.assertTrue(summary.startswith('## Release image security gate: PASS'))
        self.assertIn('**Blocking findings: 0**', summary)
        self.assertIn('**3 package-level findings**', summary)
        self.assertIn('**2 distinct CVEs**', summary)
        self.assertIn('**1 Debian source packages**', summary)
        self.assertEqual(sum(line.startswith('REVIEW HIGH |') for line in summary.splitlines()), 2)
        self.assertIn('source-one (debian) | 2 | binary-a 1, binary-b 1', summary)
        self.assertIn('HIGH | 0 | 3', summary)
        self.assertIn('no patched package available for this Debian release', summary)
        self.assertNotIn('no published fix', summary)
        self.assertIn('**NOT TRIAGED:** CVE-2026-10002', summary)
        self.assertIn('**1/2**', summary)

    def test_changed_scan_changes_counts_and_does_not_use_document_as_inventory(self):
        self.report['Results'][0]['Vulnerabilities'].pop()
        _, summary = evaluate(self.report, IMAGE_ID, self.triage)
        self.assertIn('**2 package-level findings**', summary)
        self.assertIn('**1 distinct CVEs**', summary)
        self.assertIn('HIGH | 0 | 2', summary)
        self.assertNotIn('CVE-2026-10002', summary)
        self.assertNotIn('NOT TRIAGED:', summary)

    def test_documented_fixable_high_and_critical_still_block(self):
        for severity in ('HIGH', 'CRITICAL'):
            self.report['Results'][0]['Vulnerabilities'] = [
                {**self.high('CVE-2026-10001', 'binary-a'), 'Severity': severity, 'FixedVersion': '2'}]
            blocking, summary = evaluate(self.report, IMAGE_ID, self.triage)
            self.assertEqual(blocking, 1)
            self.assertTrue(summary.startswith('## Release image security gate: FAIL'))
            self.assertIn('**Blocking findings: 1**', summary)
            self.assertIn('BLOCK ' + severity, summary)
            self.assertIn('patched version: 2', summary)
            self.assertIn('DOCUMENTED', summary)

    def test_source_and_release_scope_cannot_accidentally_mark_new_findings_triaged(self):
        for triage in ({**self.triage, 'release': '14'},
                       {**self.triage, 'entries': {'CVE-2026-10001': 'different-source'}}, None):
            blocking, summary = evaluate(self.report, IMAGE_ID, triage)
            self.assertEqual(blocking, 0)
            self.assertIn('**NOT TRIAGED:** CVE-2026-10001, CVE-2026-10002', summary)
            self.assertIn('**0/2**', summary)

    def test_missing_source_metadata_is_explicitly_untriaged(self):
        for package in self.report['Results'][0]['Packages']:
            package.pop('SrcName')
        _, summary = evaluate(self.report, IMAGE_ID, self.triage)
        self.assertIn('source unavailable:', summary)
        self.assertIn('**0 Debian source packages**', summary)
        self.assertIn('**NOT TRIAGED:** CVE-2026-10001, CVE-2026-10002', summary)

    def test_python_findings_never_claim_debian_fix_status_or_inherit_debian_triage(self):
        self.report['Results'][0]['Vulnerabilities'] = []
        self.report['Results'][1]['Vulnerabilities'] = [self.high('CVE-2026-10001', 'source-one')]
        blocking, summary = evaluate(self.report, IMAGE_ID, self.triage)
        self.assertEqual(blocking, 0)
        self.assertIn('no patched version reported by Trivy; upstream status requires review', summary)
        self.assertNotIn('no patched package available for this Debian release', summary)
        self.assertIn('**NOT TRIAGED:** CVE-2026-10001', summary)

    def test_missing_trivy_status_does_not_assert_patch_unavailability(self):
        for finding in self.report['Results'][0]['Vulnerabilities']:
            finding.pop('Status')
        _, summary = evaluate(self.report, IMAGE_ID, self.triage)
        self.assertIn('availability requires review', summary)
        self.assertNotIn('no patched package available for this Debian release', summary)

    def test_complete_counts_include_all_severities_and_both_package_types(self):
        self.report['Results'][0]['Vulnerabilities'] = []
        self.report['Results'][1]['Vulnerabilities'] = [
            self.finding(severity, fixed) for severity in ('CRITICAL', 'HIGH', 'MEDIUM', 'LOW', 'UNKNOWN')
            for fixed in ('', '2')]
        blocking, summary = evaluate(self.report, IMAGE_ID)
        self.assertEqual(blocking, 2)  # Fixable Medium/Low/Unknown remain nonblocking.
        for severity in ('CRITICAL', 'HIGH', 'MEDIUM', 'LOW', 'UNKNOWN'):
            self.assertIn(f'{severity} | 1 | 1', summary)
        self.assertIn('non-CVE High advisory IDs', summary)

    def test_explicit_index_ignores_incidental_cve_mentions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'triage.md'
            path.write_text('A future review might cover CVE-2026-10002\n' + self.index_text())
            triage = read_triage(path)
            self.assertEqual(triage, self.triage)
            _, summary = evaluate(self.report, IMAGE_ID, triage)
            self.assertIn('**NOT TRIAGED:** CVE-2026-10002', summary)
            for invalid in ('CVE-2026-10001 mentioned without index', self.index_text() * 2,
                            self.index_text({**self.triage, 'review_due': '2025-01-01'}),
                            self.index_text({**self.triage, 'entries': {'CVE-2026-10001': ['source-one']}})):
                path.write_text(invalid)
                with self.assertRaises((ValueError, TypeError)):
                    read_triage(path)

    def test_cli_triage_failure_does_not_change_policy_or_leave_stale_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report, triage = root / 'scan.json', root / 'triage.md'
            summary, receipt = root / 'summary.md', root / 'approved.json'
            command = [sys.executable, str(Path(__file__).resolve().parents[1] / 'scripts/trivy_policy.py'),
                       str(report), IMAGE_ID, '--summary', str(summary), '--receipt', str(receipt), '--triage', str(triage)]
            for content in (None, 'malformed documentation'):
                if content is not None:
                    triage.write_text(content)
                report.write_text(json.dumps(self.report))
                result = subprocess.run(command, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('**NOT TRIAGED:** CVE-2026-10001, CVE-2026-10002', summary.read_text())
                self.assertEqual(json.loads(receipt.read_text()), {
                    'image_id': IMAGE_ID, 'policy': 'fixable-high-critical', 'trivy': '0.69.3'})
            self.report['Results'][0]['Vulnerabilities'][0]['FixedVersion'] = '2'
            report.write_text(json.dumps(self.report))
            self.assertEqual(subprocess.run(command, capture_output=True, timeout=10).returncode, 1)
            self.assertFalse(receipt.exists())
            self.assertIn('**Blocking findings: 1**', summary.read_text())
            report.write_text('{')
            receipt.write_text('stale approval')
            self.assertNotEqual(subprocess.run(command, capture_output=True, timeout=10).returncode, 0)
            self.assertFalse(receipt.exists())
            self.assertIn('security gate: FAIL', summary.read_text())
            self.assertIn('Blocking findings: unknown', summary.read_text())
