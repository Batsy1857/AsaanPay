"""Integrity and security-policy negative tests; no Docker/network required."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
import subprocess
import sys
import gzip
import os
from types import SimpleNamespace
from unittest.mock import patch

from scripts import release

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
        (self.directory / "db").mkdir()
        (self.directory / "compose.prod.yaml").write_text("services: {}\n")
        (self.directory / "db/schema.sql").write_text("CREATE TABLE fixture (id INTEGER);\n")
        self.manifest = {**self.metadata, "schema_version": 1, "platform": "linux/amd64",
                         "image": "asaanpay:" + self.metadata["release_id"], "image_id": IMAGE_ID,
                         "archive": "image.tar.gz", "archive_sha256": digest(self.directory / "image.tar.gz"),
                         "local_validation": False, "source_dirty": False}
        self.write_package()

    def write_package(self):
        (self.directory / "manifest.json").write_text(json.dumps(self.manifest))
        (self.directory / "SHA256SUMS").write_text("".join(
            f"{digest(self.directory / name)}  {name}\n" for name in release.PACKAGE_FILES))

    def test_package_checksums_and_metadata(self):
        self.assertEqual(check_checksums(self.directory), self.manifest)

    def test_corrupt_archive_or_manifest_rejected(self):
        for filename in release.PACKAGE_FILES:
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

    def test_missing_package_files_rejected(self):
        for name in (*release.PACKAGE_FILES, "SHA256SUMS"):
            with self.subTest(name=name):
                path = self.directory / name
                data = path.read_bytes()
                path.unlink()
                with self.assertRaises(ValueError):
                    check_checksums(self.directory)
                path.write_bytes(data)

    def test_nested_unexpected_files_and_empty_directories_rejected(self):
        for name in ("db/extra.sql", "db/.env", "db/extra"):
            with self.subTest(name=name):
                path = self.directory / name
                if name.endswith("extra"):
                    path.mkdir()
                else:
                    path.write_text("unexpected")
                with self.assertRaises(ValueError):
                    check_checksums(self.directory)
                path.rmdir() if path.is_dir() else path.unlink()

    def test_checksum_paths_must_be_canonical_allowlisted_names(self):
        original = (self.directory / "SHA256SUMS").read_text()
        for name in ("../schema.sql", "/db/schema.sql", "db/../schema.sql", "db//schema.sql",
                     "./db/schema.sql", "db/./schema.sql", "db\\schema.sql", "SHA256SUMS", "db/extra.sql"):
            with self.subTest(name=name):
                (self.directory / "SHA256SUMS").write_text(original.replace("db/schema.sql", name))
                with self.assertRaises(ValueError):
                    check_checksums(self.directory)
        for name in release.PACKAGE_FILES:
            line = next(line for line in original.splitlines(True) if line.endswith("  " + name + "\n"))
            for text in (original.replace(line, ""), original + line):
                (self.directory / "SHA256SUMS").write_text(text)
                with self.assertRaises(ValueError):
                    check_checksums(self.directory)

    def test_all_files_and_db_directory_symlinks_rejected(self):
        with tempfile.TemporaryDirectory() as outside:
            for name in (*release.PACKAGE_FILES, "SHA256SUMS", "db"):
                with self.subTest(name=name):
                    path = self.directory / name
                    target = Path(outside) / path.name
                    path.rename(target)
                    path.symlink_to(target, target_is_directory=target.is_dir())
                    with self.assertRaises(ValueError):
                        check_checksums(self.directory)
                    path.unlink()
                    target.rename(path)
            link = Path(outside) / "package"
            link.symlink_to(self.directory, target_is_directory=True)
            with self.assertRaises(ValueError):
                check_checksums(link)
            (self.directory / "unexpected-link").symlink_to(Path(outside) / "absent")
            with self.assertRaises(ValueError):
                check_checksums(self.directory)

    def state(self):
        return {"metadata": self.metadata, "image": self.manifest["image"], "image_id": IMAGE_ID,
                "source_dirty": False, "local_validation": False, "smoke_passed": IMAGE_ID}

    def test_package_copies_deployment_files_and_is_deterministic_without_building(self):
        receipt = self.directory / "receipt.json"
        receipt.write_text(json.dumps({"image_id": IMAGE_ID, "policy": "fixable-high-critical", "trivy": "0.69.3"}))
        def save(*args, **kwargs):
            self.assertEqual(args[0], "save")
            self.assertEqual(args[-1], self.manifest["image"])
            Path(args[2]).write_bytes(b"synthetic docker save output")
        outputs = []
        with patch.object(release, "ROOT", self.directory), patch.object(release, "check_image") as identity, \
                patch.object(release, "docker", side_effect=save):
            for attempt in ("first", "second"):
                args = SimpleNamespace(output=self.directory / attempt, scan_receipt=receipt)
                release.package(args, self.state())
                destination = args.output / ("asaanpay-release-" + self.metadata["release_id"])
                check_checksums(destination)
                for name in release.DEPLOYMENT_FILES:
                    self.assertEqual((destination / name).read_bytes(), (release.ROOT / name).read_bytes())
                self.assertEqual(gzip.decompress((destination / "image.tar.gz").read_bytes()), b"synthetic docker save output")
                outputs.append({name: (destination / name).read_bytes() for name in (*release.PACKAGE_FILES, "SHA256SUMS")})
            self.assertEqual(identity.call_count, 2)
        self.assertEqual(*outputs)

    def test_package_requires_smoke_and_exact_trivy_approval(self):
        receipt = self.directory / "receipt.json"
        good = {"image_id": IMAGE_ID, "policy": "fixable-high-critical", "trivy": "0.69.3"}
        args = SimpleNamespace(output=self.directory / "output", scan_receipt=receipt)
        with patch.object(release, "check_image"), patch.object(release, "docker") as docker:
            for scan, state in ((good, {**self.state(), "smoke_passed": "wrong"}),
                                ({**good, "image_id": "wrong"}, self.state()),
                                ({**good, "policy": "other"}, self.state()),
                                ({**good, "trivy": "other"}, self.state())):
                receipt.write_text(json.dumps(scan))
                with self.assertRaises(ValueError):
                    release.package(args, state)
            docker.assert_not_called()

    def test_verify_uses_packaged_configuration_and_schema(self):
        with patch.object(release, "validate_deployment", return_value="postgres:17-alpine") as config, \
                patch.object(release, "docker") as docker, patch.object(release, "check_image") as identity, \
                patch.object(release, "stack") as stack, patch.object(release, "exercise") as exercise:
            stack.return_value.__enter__.return_value = ("url", "db", "api")
            release.verify(self.directory)
            config.assert_called_once_with(self.directory, self.manifest)
            docker.assert_called_once_with("load", "--input", str(self.directory / "image.tar.gz"), timeout=600)
            identity.assert_called_once()
            stack.assert_called_once_with(IMAGE_ID, schema=(self.directory / "db/schema.sql").resolve(),
                                          db_image="postgres:17-alpine")
            exercise.assert_called_once_with("url", "db", self.metadata)

    def test_invalid_deployment_stops_before_loading_image(self):
        with patch.object(release, "validate_deployment", side_effect=ValueError("invalid")), \
                patch.object(release, "docker") as docker:
            with self.assertRaises(ValueError):
                release.verify(self.directory)
            docker.assert_not_called()

    def deployment_config(self):
        return {"name": "asaanpay", "volumes": {"postgres_data": {"name": "asaanpay_postgres_data"}},
                "services": {"api": {"image": self.manifest["image"], "ports": [
                    {"host_ip": "127.0.0.1", "target": 8000, "published": "8000", "protocol": "tcp"}]},
                    "db": {"image": "postgres:17-alpine", "volumes": [
                        {"type": "bind", "read_only": True,
                         "source": str((self.directory / "db/schema.sql").resolve()),
                         "target": "/docker-entrypoint-initdb.d/001-schema.sql"},
                        {"type": "volume", "source": "postgres_data", "target": "/var/lib/postgresql/data"}]}}}

    def test_compose_validation_uses_dummy_environment_and_checks_release_bindings(self):
        config = self.deployment_config()
        with patch.dict(os.environ, {"POSTGRES_PASSWORD": "must-not-leak", "COMPOSE_FILE": "other.yml"}), \
                patch.object(release, "docker", return_value=json.dumps(config)) as docker:
            self.assertEqual(release.validate_deployment(self.directory, self.manifest), "postgres:17-alpine")
            args, kwargs = docker.call_args
            self.assertEqual(args[-3:], ("config", "--format", "json"))
            self.assertIn(str((self.directory / "compose.prod.yaml").resolve()), args)
            self.assertEqual(args[1:3], ("--env-file", "/dev/null"))
            self.assertEqual(kwargs["env"]["POSTGRES_PASSWORD"], "dummy-validation-only")
            self.assertNotIn("COMPOSE_FILE", kwargs["env"])
        variants = []
        for key, value in (("image", "asaanpay:latest"), ("build", "."), ("platform", "linux/arm64")):
            modified = deepcopy(config)
            modified["services"]["api"][key] = value
            variants.append(modified)
        for key, value in (("source", str(release.ROOT / "db/schema.sql")), ("read_only", False)):
            modified = deepcopy(config)
            modified["services"]["db"]["volumes"][0][key] = value
            variants.append(modified)
        for modified in variants:
            with patch.object(release, "docker", return_value=json.dumps(modified)), self.assertRaises(ValueError):
                release.validate_deployment(self.directory, self.manifest)

    def test_compose_safe_normalized_defaults(self):
        for explicit in (False, True):
            config = self.deployment_config()
            if explicit:
                for service in config["services"].values():
                    service.update(privileged=False, cap_add=[], devices=[], network_mode=None,
                                   pid="private", ipc="shareable", use_api_socket=False,
                                   volumes_from=[], tmpfs=[], configs=[], secrets=[])
                config["services"]["db"]["ports"] = []
                config["services"]["api"]["volumes"] = []
                config["services"]["api"]["ports"][0].update(target="8000", published=8000, mode="ingress")
                config["services"]["api"]["ports"][0].pop("protocol")
                config["volumes"]["postgres_data"].update(driver="local", driver_opts={}, external=False)
                config["networks"] = {"default": {"driver": "bridge", "external": False}}
                config["services"]["db"]["volumes"][1].update(read_only=False, volume={})
            with self.subTest(explicit=explicit), patch.object(release, "docker", return_value=json.dumps(config)):
                self.assertEqual(release.validate_deployment(self.directory, self.manifest), "postgres:17-alpine")

    def test_checksum_valid_unsafe_compose_is_rejected_before_image_load(self):
        # Each mutation starts from a valid config; checksums are regenerated to
        # demonstrate that deployment policy is independent of file integrity.
        mutations = [
            (("name",), "other"),
            (("services", "db", "ports"), [{"target": 5432, "published": "5432", "host_ip": "127.0.0.1"}]),
            (("services", "api", "ports"), []),
            (("services", "api", "ports", 0, "host_ip"), None),
            (("services", "api", "ports", 0, "host_ip"), "0.0.0.0"),
            (("services", "api", "ports", 0, "host_ip"), "::"),
            (("services", "api", "ports", 0, "target"), 5432),
            (("services", "api", "ports", 0, "published"), "9000"),
            (("services", "api", "ports", 0, "protocol"), "udp"),
            (("services", "api", "ports"), self.deployment_config()["services"]["api"]["ports"] * 2),
            (("volumes", "postgres_data", "name"), "other_data"),
            (("volumes", "postgres_data", "driver_opts"), {"type": "none", "o": "bind", "device": "/"}),
            (("volumes", "postgres_data", "driver"), "other"),
            (("volumes", "postgres_data", "external"), True),
            (("services", "db", "volumes", 1, "source"), None),
            (("services", "db", "volumes", 1, "source"), "other_data"),
            (("services", "db", "volumes", 1, "type"), "bind"),
            (("services", "db", "volumes", 1, "type"), "tmpfs"),
            (("services", "db", "volumes", 1, "target"), "/wrong"),
            (("services", "db", "volumes", 1, "read_only"), True),
            (("services", "db", "volumes", 1, "volume"), {"subpath": "other"}),
            (("services", "db", "volumes"), self.deployment_config()["services"]["db"]["volumes"][:1]),
            (("networks",), {"default": {"driver": "host"}}),
            (("networks",), {"default": {"external": True, "name": "host"}}),
        ]
        for name in ("api", "db"):
            for field, value in (("privileged", True), ("cap_add", ["SYS_ADMIN"]),
                                 ("network_mode", "host"), ("network_mode", "container:host-proxy"),
                                 ("pid", "host"), ("ipc", "host"), ("ipc", "service:other"),
                                 ("devices", ["/dev/sda:/dev/sda"]), ("device_cgroup_rules", ["a *:* rwm"]),
                                 ("gpus", "all"), ("use_api_socket", True), ("volumes_from", ["other"]),
                                 ("tmpfs", ["/var/lib/postgresql/data"]),
                                 ("configs", [{"source": "socket"}]), ("secrets", [{"source": "socket"}]),
                                 ("post_start", [{"command": "id", "privileged": True}]),
                                 ("deploy", {"resources": {"reservations": {"devices": [{"capabilities": ["gpu"]}]}}})):
                mutations.append((("services", name, field), value))
            for source, target in (("/etc", "/host"), ("/var/run/docker.sock", "/var/run/docker.sock")):
                mounts = deepcopy(self.deployment_config()["services"][name].get("volumes", []))
                mounts.append({"type": "bind", "source": source, "target": target, "read_only": True})
                mutations.append((("services", name, "volumes"), mounts))
        for path, value in mutations:
            config = self.deployment_config()
            node = config
            for key in path[:-1]:
                node = node[key]
            node[path[-1]] = value
            with self.subTest(path=path, value=value):
                (self.directory / "compose.prod.yaml").write_text(json.dumps(config))
                self.write_package()
                check_checksums(self.directory)
                with patch.object(release, "docker", return_value=json.dumps(config)) as docker:
                    with self.assertRaises(ValueError):
                        release.verify(self.directory)
                    self.assertEqual(docker.call_count, 1)
                    self.assertEqual(docker.call_args.args[0], "compose")

    def test_stack_mounts_selected_schema_and_never_uses_named_volumes(self):
        schema = self.directory / "db/schema.sql"
        with patch.object(release, "docker", return_value="127.0.0.1:12345") as docker, \
                patch.object(release, "wait_for"):
            with release.stack(IMAGE_ID, schema=schema):
                pass
        db_create = next(call.args for call in docker.call_args_list
                         if call.args[0] == "create" and "postgres:17-alpine" in call.args)
        self.assertIn(f"type=bind,src={schema},dst=/docker-entrypoint-initdb.d/001-schema.sql,readonly", db_create)
        self.assertIn("/var/lib/postgresql/data:rw", db_create)
        self.assertIn("--tmpfs", db_create)
        self.assertFalse(any(call.args[0] == "volume" for call in docker.call_args_list))


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
