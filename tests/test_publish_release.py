"""Publication safety tests; every AWS interaction is mocked."""

from contextlib import redirect_stdout
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import publish_release as publisher
from scripts import release


class PublishReleaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'VERSION').write_text('1.0.4\n')
        self.directory = self.root / 'package'
        self.directory.mkdir()
        (self.directory / 'db').mkdir()
        self.expected = release.make_metadata('1.0.4', 'a' * 40, '123', '2')
        self.environment = {'GITHUB_EVENT_NAME': 'push', 'GITHUB_REPOSITORY': 'Batsy1857/AsaanPay',
                            'GITHUB_REF': 'refs/heads/feature/aws-cicd-devsecops', 'GITHUB_SHA': 'a' * 40,
                            'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '2'}
        for name in ('image.tar.gz', 'compose.prod.yaml', 'db/schema.sql'):
            (self.directory / name).write_bytes(b'synthetic fixture: ' + name.encode())
        self.manifest = {**self.expected, 'schema_version': 1, 'platform': 'linux/amd64',
                         'image': 'asaanpay:' + self.expected['release_id'], 'image_id': 'sha256:' + 'b' * 64,
                         'archive': 'image.tar.gz', 'archive_sha256': release.digest(self.directory / 'image.tar.gz'),
                         'local_validation': False, 'source_dirty': False}
        self.write_package()
        self.objects, self.calls = {}, []
        self.failure = self.corrupt = None
        self.output = io.StringIO()
        self.real_aws = publisher.aws
        self.aws_patch = patch.object(publisher, 'aws', side_effect=self.fake_aws)
        self.aws_mock = self.aws_patch.start()
        self.addCleanup(self.aws_patch.stop)

    def write_package(self):
        release.write_json(self.directory / 'manifest.json', self.manifest)
        (self.directory / 'SHA256SUMS').write_text(''.join(
            f'{release.digest(self.directory / name)}  {name}\n' for name in release.PACKAGE_FILES))

    def fake_aws(self, *args):
        self.calls.append(args)
        if args[:2] == ('sts', 'get-caller-identity'):
            return json.dumps({'Account': publisher.ACCOUNT,
                              'Arn': f'arn:aws:sts::{publisher.ACCOUNT}:assumed-role/AsaanPay-GitHubActions-Role/AsaanPay-Publish-123-2'})
        self.assertEqual(args[0], 's3api')
        operation = args[1]
        key = args[args.index('--key') + 1]
        name = key.removeprefix('releases/' + self.expected['release_id'] + '/')
        self.assertIn(name, publisher.FILES)
        self.assertEqual(args[args.index('--bucket') + 1], publisher.BUCKET)
        self.assertEqual(args[args.index('--expected-bucket-owner') + 1], publisher.ACCOUNT)
        if self.failure == (operation, name):
            raise subprocess.CalledProcessError(254, ['aws'], stderr='AccessDenied: explicit deny on ' + name)
        if operation == 'put-object':
            self.assertEqual(args[args.index('--if-none-match') + 1], '*')
            if key in self.objects:
                raise subprocess.CalledProcessError(254, ['aws'], stderr='PreconditionFailed (412)')
            content = Path(args[args.index('--body') + 1]).read_bytes()
            import base64
            self.assertEqual(args[args.index('--checksum-sha256') + 1],
                             base64.b64encode(hashlib.sha256(content).digest()).decode())
            self.objects[key] = content
        elif operation == 'get-object':
            Path(args[-1]).write_bytes(b'corrupt' if self.corrupt == name else self.objects[key])
        else:
            self.fail('Unexpected AWS operation: ' + operation)
        return '{}'

    def publish(self):
        with redirect_stdout(self.output), patch.object(release, 'validate_deployment') as deployment:
            publisher.publish(self.directory, self.expected)
        deployment.assert_called_once()

    def test_exact_run_identity_and_branch_restrictions(self):
        with patch.object(release, 'ROOT', self.root):
            self.assertEqual(publisher.expected_identity(self.environment), self.expected)
            for key, value in (('GITHUB_EVENT_NAME', 'pull_request'), ('GITHUB_EVENT_NAME', 'workflow_dispatch'),
                               ('GITHUB_REF', 'refs/heads/main'), ('GITHUB_REF', 'refs/tags/feature/aws-cicd-devsecops'),
                               ('GITHUB_REPOSITORY', 'other/AsaanPay'), ('GITHUB_REPOSITORY', 'batsy1857/AsaanPay'),
                               ('GITHUB_SHA', '../unsafe'), ('GITHUB_RUN_ATTEMPT', '0')):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    publisher.expected_identity({**self.environment, key: value})

    def test_identity_image_tag_and_provenance_mismatches(self):
        original = deepcopy(self.manifest)
        for key, value in (('commit', 'c' * 40), ('run_id', '124'), ('run_attempt', '1'),
                           ('version', '1.0.5'), ('release_id', '../unsafe'), ('image', 'asaanpay:latest'),
                           ('archive_sha256', '0' * 64), ('local_validation', True), ('source_dirty', True)):
            self.manifest = {**original, key: value}
            self.write_package()
            with self.subTest(key=key), self.assertRaises(ValueError):
                publisher.validate_package(self.directory, self.expected)
        # A self-consistent artifact from another attempt must still be rejected.
        other = release.make_metadata('1.0.4', 'a' * 40, '123', '1')
        self.manifest = {**original, **other, 'image': 'asaanpay:' + other['release_id']}
        self.write_package()
        with self.assertRaisesRegex(ValueError, 'identity differs'):
            publisher.validate_package(self.directory, self.expected)
        self.aws_mock.assert_not_called()

    def test_missing_unexpected_and_tampered_files_stop_before_aws(self):
        for name in publisher.FILES:
            path = self.directory / name
            original = path.read_bytes()
            for corruption in (None, original + b'tampered'):
                path.unlink() if corruption is None else path.write_bytes(corruption)
                with self.subTest(name=name, corruption=corruption is None), self.assertRaises((ValueError, UnicodeError)):
                    self.publish()
                path.write_bytes(original)
        (self.directory / 'db/extra.sql').write_text('unexpected')
        with self.assertRaises(ValueError):
            self.publish()
        self.aws_mock.assert_not_called()

    def test_single_put_size_boundaries(self):
        largest = max((self.directory / name).stat().st_size for name in publisher.FILES)
        with patch.object(publisher, 'MAX_PUT_BYTES', largest):
            publisher.validate_package(self.directory, self.expected)
        with patch.object(publisher, 'MAX_PUT_BYTES', largest - 1), self.assertRaisesRegex(ValueError, 'size limit'):
            self.publish()
        self.aws_mock.assert_not_called()

    def test_success_uploads_and_verifies_every_file_with_completion_last(self):
        self.publish()
        operations = [(args[1], args[args.index('--key') + 1].split('/')[-1]) for args in self.calls[1:]]
        self.assertEqual(operations, [(operation, Path(name).name) for name in publisher.FILES
                                     for operation in ('put-object', 'get-object')])
        self.assertEqual(operations[-2:], [('put-object', 'SHA256SUMS'), ('get-object', 'SHA256SUMS')])
        self.assertEqual(len(self.objects), 5)
        self.assertIn('releases/' + self.expected['release_id'] + '/db/schema.sql', self.objects)
        self.assertIn('PASS: all 5 S3 files', self.output.getvalue())

    def test_wrong_sts_identity_never_touches_s3(self):
        for identity in ({'Account': '000000000000', 'Arn': 'wrong'},
                         {'Account': publisher.ACCOUNT, 'Arn': 'arn:aws:iam::642195693459:user/wrong'},
                         {'Account': publisher.ACCOUNT, 'Arn': f'arn:aws:sts::{publisher.ACCOUNT}:assumed-role/OtherRole/AsaanPay-Publish-123-2'},
                         {'Account': publisher.ACCOUNT, 'Arn': f'arn:aws:sts::{publisher.ACCOUNT}:assumed-role/AsaanPay-GitHubActions-Role/OtherSession'}):
            with patch.object(publisher, 'aws', return_value=json.dumps(identity)) as aws, self.assertRaises(ValueError):
                self.publish()
            aws.assert_called_once_with('sts', 'get-caller-identity')

    def test_failed_uploads_and_downloads_never_report_success(self):
        for operation in ('put-object', 'get-object'):
            for name in publisher.FILES:
                self.calls.clear()
                self.objects.clear()
                self.failure = operation, name
                with self.subTest(operation=operation, name=name), self.assertRaises(subprocess.CalledProcessError) as error:
                    self.publish()
                self.assertIn('AccessDenied', error.exception.stderr)
                self.assertEqual(self.calls[-1][1], operation)
                if name != 'SHA256SUMS':
                    self.assertFalse(any(key.endswith('/SHA256SUMS') for key in self.objects))
                self.assertNotIn('PASS:', self.output.getvalue())

    def test_checksum_mismatch_prevents_completion_and_false_success(self):
        for name in publisher.FILES:
            self.objects.clear()
            self.corrupt = name
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'SHA-256 mismatch'):
                self.publish()
            if name != 'SHA256SUMS':
                self.assertFalse(any(key.endswith('/SHA256SUMS') for key in self.objects))
            self.assertNotIn('PASS:', self.output.getvalue())

    def test_existing_objects_including_completion_are_never_overwritten(self):
        for name in publisher.FILES:
            key = 'releases/' + self.expected['release_id'] + '/' + name
            self.objects.clear()
            self.objects[key] = b'pre-existing object'
            with self.subTest(name=name), self.assertRaises(subprocess.CalledProcessError) as error:
                self.publish()
            self.assertIn('412', error.exception.stderr)
            self.assertEqual(self.objects[key], b'pre-existing object')
            self.assertNotIn('PASS:', self.output.getvalue())

    def test_conflict_has_no_retry_or_unconditional_fallback(self):
        with patch.object(publisher, 'aws', side_effect=[self.fake_aws('sts', 'get-caller-identity'),
                subprocess.CalledProcessError(254, ['aws'], stderr='ConditionalRequestConflict (409)')]) as aws:
            with self.assertRaises(subprocess.CalledProcessError):
                self.publish()
            self.assertEqual(aws.call_count, 2)
            self.assertIn('--if-none-match', aws.call_args.args)
        self.assertNotIn('PASS:', self.output.getvalue())

    def test_complete_download_is_validated_again_before_success(self):
        original = publisher.validate_package
        def validate(directory, expected):
            if directory != self.directory:
                raise ValueError('final validation failed')
            return original(directory, expected)
        with patch.object(publisher, 'validate_package', side_effect=validate), self.assertRaisesRegex(ValueError, 'final validation'):
            self.publish()
        self.assertEqual(len(self.objects), 5)
        self.assertNotIn('PASS:', self.output.getvalue())

    def test_check_command_validates_before_auth_without_aws_or_image_loading(self):
        with patch.dict(os.environ, self.environment), patch.object(release, 'ROOT', self.root), \
                patch.object(release, 'validate_deployment') as deployment, \
                patch.object(sys, 'argv', ['publish_release.py', 'check', str(self.directory)]):
            publisher.main()
        deployment.assert_called_once()
        self.aws_mock.assert_not_called()

    def test_summary_does_not_claim_success_for_failed_or_skipped_publication(self):
        summary = self.root / 'summary.md'
        for outcome in ('failure', 'skipped', 'cancelled', 'success'):
            summary.write_text('')
            with patch.dict(os.environ, {**self.environment, 'PUBLISH_RESULT': outcome, 'GITHUB_STEP_SUMMARY': str(summary)}), \
                    patch.object(release, 'ROOT', self.root), patch.object(sys, 'argv', ['publish_release.py', 'summary']):
                publisher.main()
            self.assertEqual('Verification: PASS' in summary.read_text(), outcome == 'success')
            self.assertIn('Expected file count: 5', summary.read_text())

    def test_prepare_selects_exact_version_commit_run_and_attempt_artifact(self):
        output = self.root / 'outputs.txt'
        with patch.dict(os.environ, {**self.environment, 'GITHUB_OUTPUT': str(output)}), \
                patch.object(release, 'ROOT', self.root), patch.object(sys, 'argv', ['publish_release.py', 'prepare']):
            publisher.main()
        self.assertEqual(output.read_text(), 'artifact-name=asaanpay-release-' + self.expected['release_id'] + '\n')
        self.aws_mock.assert_not_called()

    def test_aws_cli_has_no_retry_and_preserves_failure_diagnostics(self):
        error = subprocess.CalledProcessError(254, ['aws'], stderr='AccessDenied: exact service error')
        with patch.object(subprocess, 'run', side_effect=error) as run:
            with self.assertRaises(subprocess.CalledProcessError) as caught:
                self.real_aws('s3api', 'put-object', '--if-none-match', '*')
        self.assertIs(caught.exception, error)
        self.assertEqual(run.call_args.kwargs['env']['AWS_MAX_ATTEMPTS'], '1')
        self.assertTrue(run.call_args.kwargs['check'])
        self.assertTrue(run.call_args.kwargs['capture_output'])
        self.assertIn('us-east-1', run.call_args.args[0])
