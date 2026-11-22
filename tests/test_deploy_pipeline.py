"""Automatic deployment safety: synthetic policies, mocked AWS, no live resources."""
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts import deploy_pipeline as pipeline
from scripts import ssm_deploy as host
from scripts import deploy_ec2 as deploy
from scripts import release
import test_deploy_ec2 as deployment_fixtures
from test_deploy_ec2 import fixture, compose_config
import demo_fixtures


class PipelineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root / 'package'
        self.manifest = fixture(self.directory)
        self.expected = deploy.validate_manifest(self.manifest)
        (self.root / 'VERSION').write_text('1.0.4\n')
        self.env = {'GITHUB_EVENT_NAME': 'push', 'GITHUB_REPOSITORY': pipeline.REPOSITORY,
                    'GITHUB_REF': 'refs/heads/' + pipeline.BRANCH, 'GITHUB_SHA': 'a' * 40,
                    'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_RUN_NUMBER': '7',
                    'PUBLISHED_RELEASE_ID': self.expected['release_id'], 'PUBLICATION_RESULT': 'success',
                    'PUBLICATION_SHA256': deploy.digest(self.directory / 'SHA256SUMS'),
                    'PRODUCTION_DEPLOYMENT_ENABLED': 'false', 'RUNNER_TEMP': str(self.root)}
        self.config = compose_config(self.directory, self.manifest)
        self.facts = host.contract_facts(self.directory, self.config)
        self.checker = self.root / 'checker.py'
        self.checker.write_text('# Synthetic test checker; never a production approval.\n')
        self.policy = demo_fixtures.policy(self.facts, self.expected['release_id'])
        self.policy['contract']['checker_sha256'] = deploy.digest(self.checker)
        self.policy_path = self.root / 'policy.json'
        self.policy_path.write_text(json.dumps(self.policy))
        self.policy_sha = deploy.digest(self.policy_path)
        self.evidence = {'schema_version': 1, 'passed': True, 'release_id': self.expected['release_id'],
                         'image_id': self.manifest['image_id'], 'policy_sha256': self.policy_sha,
                         'contract_id': 'fixture-only', 'live_schema_sha256':self.policy['contract']['live_schema_sha256'],
                         'checks': {key:True for key in host.DEMO_CHECKS}}
        self.request = {'expected': self.expected, 'image_id': self.manifest['image_id'],
                        'facts': self.facts, 'policy_sha256': self.policy_sha, 'evidence': self.evidence,
                        'script_sha256': deploy.digest(pipeline.ROOT / 'scripts/deploy_ec2.py'),
                        'marker_sha256': self.env['PUBLICATION_SHA256'], 'workflow_id': 42, 'run_number': 7,
                        'repository': pipeline.REPOSITORY, 'branch': pipeline.BRANCH, 'nonce': 'b' * 32}
        self.calls = []
        self.objects = {'releases/' + self.expected['release_id'] + '/' + name: (self.directory / name).read_bytes()
                        for name in deploy.FILES}
        self.patch(release, 'ROOT', self.root)
        # Parity on Python 3.9: release.py is CI-only; its digest uses Python 3.11+.
        self.patch(release, 'digest', deploy.digest)
        self.patch(pipeline, 'POLICY', self.policy_path)
        self.patch(pipeline, 'CHECKER', self.checker)
        self.aws = self.patch(pipeline, 'aws', side_effect=self.fake_aws)
        self.github = self.patch(pipeline, 'github', side_effect=self.fake_github)
        self.run = {'id': 123, 'run_attempt': 1, 'run_number': 7, 'head_sha': 'a' * 40,
                    'head_branch': pipeline.BRANCH, 'event': 'push', 'path': '.github/workflows/ci.yml',
                    'repository': {'full_name': pipeline.REPOSITORY}, 'head_repository': {'full_name': pipeline.REPOSITORY},
                    'workflow_id': 42, 'status': 'in_progress', 'conclusion': None}
        self.jobs = [{'name': name, 'status': 'completed', 'conclusion': 'success'} for name in pipeline.REQUIRED_JOBS]
        self.head = {'object': {'type': 'commit', 'sha': 'a' * 40}}
        self.command_id = '12345678-1234-4234-8234-123456789abc'
        self.invocation = {'CommandId': self.command_id, 'InstanceId': pipeline.INSTANCE, 'DocumentName': 'AWS-RunShellScript',
                           'Status': 'Success', 'ResponseCode': 0, 'StandardOutputContent': 'ASAANPAY_DEPLOY_OK ' + self.request['nonce']}

    def patch(self, obj, name, *args, **kwargs):
        p = patch.object(obj, name, *args, **kwargs)
        value = p.start()
        self.addCleanup(p.stop)
        return value

    def test_contract_normalizes_only_equivalent_compose_defaults(self):
        equivalent = deepcopy(self.config)
        health = equivalent['services']['db']['healthcheck'] = {
            'test': ['CMD-SHELL', 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"']}
        expected = host.contract_facts(self.directory, equivalent)
        health['test'] = ['CMD-SHELL', 'pg_isready -U "$$POSTGRES_USER" -d "$$POSTGRES_DB"']
        self.assertEqual(host.contract_facts(self.directory, equivalent), expected)
        self.assertEqual(host.json_hash({'default': {'empty': None}}), host.json_hash({}))
        self.assertEqual(host.canonical(host.canonical(equivalent)), host.canonical(equivalent))
        health['test'] = ['CMD-SHELL', 'true']
        self.assertNotEqual(host.contract_facts(self.directory, equivalent), expected)
        changed = deepcopy(self.config)
        changed['networks'] = {'default': {'internal': True}}
        self.assertNotEqual(host.contract_facts(self.directory, changed), self.facts)

    def fake_github(self, path):
        if path == '/actions/runs/123/attempts/1':
            return self.run
        if path == '/actions/runs/123/attempts/1/jobs?per_page=100&page=1':
            return {'jobs': self.jobs}
        if path == '/git/ref/heads/' + pipeline.BRANCH:
            return self.head
        self.fail('Unexpected GitHub operation: ' + path)

    def fake_aws(self, service, operation, *args, **kwargs):
        self.calls.append((service, operation, args, kwargs))
        if service == 's3api':
            self.assertEqual(args[args.index('--bucket') + 1], 'asaanpay-releases')
            self.assertEqual(args[args.index('--expected-bucket-owner') + 1], deploy.ACCOUNT)
            key = args[args.index('--key') + 1]
            if operation == 'put-object':
                self.assertEqual(args[args.index('--if-none-match') + 1], '*')
                if key in self.objects:
                    raise pipeline.AWSFailure('PreconditionFailed')
                self.objects[key] = Path(args[args.index('--body') + 1]).read_bytes()
                return {}
            if key not in self.objects:
                raise pipeline.AWSFailure('NoSuchKey')
            data = self.objects[key]
            etag = '"' + host.digest(data) + '"'
            if operation == 'get-object':
                self.assertEqual(args[args.index('--if-match') + 1], etag)
                self.assertEqual(args[args.index('--range') + 1], 'bytes=0-' + str(len(data) - 1))
                Path(args[-1]).write_bytes(data)
            else:
                self.assertEqual(operation, 'head-object')
            return {'ContentLength': len(data), 'ETag': etag}
        if (service, operation) == ('sts', 'get-caller-identity'):
            return {'Account': deploy.ACCOUNT, 'Arn': 'arn:aws:sts::' + deploy.ACCOUNT +
                    ':assumed-role/AsaanPay-GitHubActions-Role/AsaanPay-Deploy-123-1'}
        if (service, operation) == ('ssm', 'describe-instance-information'):
            self.assertEqual(json.loads(args[1]), [{'Key': 'InstanceIds', 'Values': [pipeline.INSTANCE]}])
            return {'InstanceInformationList': [{'InstanceId': pipeline.INSTANCE, 'PingStatus': 'Online',
                                                'PlatformType': 'Linux', 'ResourceType': 'EC2Instance'}]}
        if (service, operation) == ('ssm', 'send-command'):
            self.assertEqual(args[args.index('--instance-ids') + 1], pipeline.INSTANCE)
            self.assertEqual(args[args.index('--document-name') + 1], 'AWS-RunShellScript')
            return {'Command': {'CommandId': self.command_id}}
        if (service, operation) == ('ssm', 'get-command-invocation'):
            self.assertEqual(args[args.index('--instance-id') + 1], pipeline.INSTANCE)
            self.assertEqual(args[args.index('--command-id') + 1], self.command_id)
            return self.invocation
        self.fail('Unexpected AWS operation: ' + service + '/' + operation)

    def authorize(self):
        def checker(args, **kwargs):
            self.assertEqual(args[:2], [sys.executable, str(self.checker)])
            self.assertLessEqual(set(kwargs['env']), {'PATH', 'HOME'})
            self.assertEqual(kwargs['timeout'], 480)
            Path(args[args.index('--output') + 1]).write_text(json.dumps(self.evidence))
            return subprocess.CompletedProcess(args, 0)
        with patch.object(pipeline, 'configuration', return_value=self.config), patch.object(pipeline.subprocess, 'run', side_effect=checker):
            return pipeline.authorize(self.directory, {**self.env, 'PRODUCTION_DEPLOYMENT_ENABLED': 'true'})

    def test_exact_release_outputs_and_inputs(self):
        proof = pipeline.record_publication(self.directory, {**self.env, 'BUILT_RELEASE_ID': self.expected['release_id']})
        self.assertEqual(proof, {'release-id': self.expected['release_id'], 'marker-sha256': self.env['PUBLICATION_SHA256']})
        self.assertEqual(pipeline.inputs(self.env), self.expected)
        self.aws.assert_not_called()

    def test_wrong_build_output_rejected(self):
        with self.assertRaises(deploy.DeploymentError):
            pipeline.record_publication(self.directory, {**self.env, 'BUILT_RELEASE_ID': 'latest'})

    def test_identity_branch_event_repository_restrictions(self):
        for key, bad in [('GITHUB_REF', 'refs/heads/main'), ('GITHUB_REPOSITORY', 'fork/AsaanPay'),
                         ('GITHUB_EVENT_NAME', 'pull_request'), ('GITHUB_EVENT_NAME', 'workflow_dispatch'),
                         ('GITHUB_RUN_ATTEMPT', '2'), ('GITHUB_SHA', 'c' * 40), ('PUBLICATION_RESULT', 'failure'),
                         ('PUBLISHED_RELEASE_ID', '../../unsafe;echo bad'), ('PUBLICATION_SHA256', ''), ('GITHUB_RUN_NUMBER', '0')]:
            with self.subTest(key=key, bad=bad), self.assertRaises((ValueError, deploy.DeploymentError)):
                pipeline.inputs({**self.env, key: bad})
        self.aws.assert_not_called()

    def test_package_corruption_missing_and_unexpected_files(self):
        for name in deploy.FILES:
            path = self.directory / name
            saved = path.read_bytes()
            for content in (None, saved + b'changed'):
                if content is None:
                    path.unlink()
                else:
                    path.write_bytes(content)
                with self.subTest(name=name, missing=content is None), self.assertRaises((ValueError, OSError)):
                    pipeline.record_publication(self.directory, {**self.env, 'BUILT_RELEASE_ID': self.expected['release_id']})
                path.write_bytes(saved)
        (self.directory / 'unexpected').write_text('extra')
        with self.assertRaises(ValueError):
            pipeline.record_publication(self.directory, {**self.env, 'BUILT_RELEASE_ID': self.expected['release_id']})
        self.aws.assert_not_called()

    def test_disabled_gate_never_authenticates_or_deploys(self):
        for func, args in [(pipeline.authorize, (self.directory, self.env)),
                           (pipeline.execute, (self.directory, self.request, self.env, {}))]:
            with self.assertRaisesRegex(deploy.DeploymentError, 'disabled'):
                func(*args)
        with patch.dict(os.environ, {**self.env, 'GITHUB_STEP_SUMMARY': str(self.root / 'summary')}), \
                patch.object(sys, 'argv', ['deploy_pipeline.py', 'prepare', str(self.directory)]):
            self.assertEqual(pipeline.main(), 0)
        summary = (self.root / 'summary').read_text()
        self.assertIn('Production gate disabled', summary)
        self.assertIn('NOT VERIFIED', summary)
        self.aws.assert_not_called()
        self.github.assert_not_called()

    def test_uncommissioned_policy_blocks_even_with_workflow_gate_enabled(self):
        self.policy_path.write_text(json.dumps({'schema_version': 1, 'commissioned': False, 'approval': None, 'contract': None}))
        with self.assertRaisesRegex(deploy.DeploymentError, 'not commissioned'):
            self.authorize()
        self.aws.assert_not_called()

    def test_real_checker_protocol_accepts_exact_evidence(self):
        value = self.authorize()
        self.assertEqual(value['facts'], self.facts)
        self.assertEqual(value['evidence'], self.evidence)
        self.assertEqual(value['marker_sha256'], self.env['PUBLICATION_SHA256'])
        self.aws.assert_not_called()

    def test_missing_or_changed_checker_blocks(self):
        self.checker.write_text('changed')
        with self.assertRaisesRegex(deploy.DeploymentError, 'checker is missing or changed'):
            self.authorize()
        self.checker.unlink()
        with self.assertRaises(deploy.DeploymentError):
            self.authorize()

    def test_checker_nonzero_fails_without_aws(self):
        with patch.object(pipeline, 'configuration', return_value=self.config), \
                patch.object(pipeline.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, ['checker'])), \
                self.assertRaises(subprocess.CalledProcessError):
            pipeline.authorize(self.directory, {**self.env, 'PRODUCTION_DEPLOYMENT_ENABLED': 'true'})
        self.aws.assert_not_called()

    def test_sql_equality_does_not_substitute_for_compatibility_evidence(self):
        for key, bad in [('passed', False), ('checks', {}), ('checks', {'existing-data': 1}),
                         ('release_id', 'old'), ('image_id', 'sha256:' + 'f' * 64),
                         ('policy_sha256', 'f' * 64), ('contract_id', 'other')]:
            bad_value = {**self.evidence, key: bad}
            with self.subTest(key=key, bad=bad), self.assertRaises(deploy.DeploymentError):
                pipeline.check_evidence(bad_value, self.expected, self.manifest, self.policy, self.policy_sha)
            with self.subTest(host=key), self.assertRaises(ValueError):
                host.check_policy({**self.request, 'evidence': bad_value}, self.policy)

    def test_each_database_contract_change_is_rejected(self):
        for key in self.facts:
            changed = {**self.facts, key: 'changed'}
            with self.subTest(key=key), self.assertRaises(deploy.DeploymentError):
                pipeline.check_contract(changed, self.policy)
            with self.subTest(host=key), self.assertRaises(ValueError):
                host.check_policy({**self.request, 'facts': changed}, self.policy)

    def test_contract_normalizes_only_path_credentials_and_serializer_defaults(self):
        config = deepcopy(self.config)
        config['services']['db']['environment']['POSTGRES_PASSWORD'] = 'different-secret'
        config['services']['db']['volumes'][1]['source'] = '/another/package/db/schema.sql'
        config['services']['db']['platform'] = 'linux/amd64'
        config['services']['db']['ports'] = []
        self.assertEqual(host.contract_facts(self.directory, config), self.facts)
        config['services']['db']['privileged'] = True
        self.assertNotEqual(host.contract_facts(self.directory, config), self.facts)

    def test_in_progress_run_with_all_successful_prerequisites_is_valid(self):
        self.assertEqual(pipeline.provenance(self.expected, self.env), 42)

    def test_wrong_provenance_and_unsuccessful_jobs_block(self):
        original = deepcopy(self.run)
        for key, bad in [('id', 124), ('run_attempt', 2), ('run_number', 8), ('head_sha', 'f' * 40),
                         ('head_branch', 'main'), ('event', 'pull_request'), ('path', '.github/workflows/other.yml'),
                         ('repository', {'full_name': 'fork/AsaanPay'}), ('head_repository', {'full_name': 'fork/AsaanPay'})]:
            self.run = {**original, key: bad}
            with self.subTest(key=key), self.assertRaises(deploy.DeploymentError):
                pipeline.provenance(self.expected, self.env)
        self.run = original
        for job in self.jobs:
            for conclusion in ('failure', 'skipped', 'cancelled', None):
                job['conclusion'] = conclusion
                with self.subTest(job=job['name'], result=conclusion), self.assertRaises(deploy.DeploymentError):
                    pipeline.provenance(self.expected, self.env)
            job['conclusion'] = 'success'
        self.jobs.append(dict(self.jobs[0]))
        with self.assertRaises(deploy.DeploymentError):
            pipeline.provenance(self.expected, self.env)

    def test_newer_failed_commit_does_not_block_successful_originating_run(self):
        self.head['object']['sha'] = 'f' * 40
        self.assertEqual(pipeline.provenance(self.expected, self.env), 42)
        self.assertFalse(any('/git/ref/' in call.args[0] for call in self.github.call_args_list))

    def test_complete_publication_marker_and_all_files_required(self):
        pipeline.verify_publication(self.directory, self.expected, self.env['PUBLICATION_SHA256'])
        for key in list(self.objects):
            saved = self.objects.pop(key)
            with self.subTest(key=key), self.assertRaises(pipeline.AWSFailure):
                pipeline.verify_publication(self.directory, self.expected, self.env['PUBLICATION_SHA256'])
            self.objects[key] = saved

    def test_changed_s3_marker_or_manifest_blocks(self):
        for name in ('SHA256SUMS', 'manifest.json'):
            key = 'releases/' + self.expected['release_id'] + '/' + name
            saved = self.objects[key]
            self.objects[key] = b'!' + saved[1:]
            with self.subTest(name=name), self.assertRaisesRegex(deploy.DeploymentError, 'differs'):
                pipeline.verify_publication(self.directory, self.expected, self.env['PUBLICATION_SHA256'])
            self.objects[key] = saved

    def test_tool_delivery_is_conditional_and_checks_existing_bytes(self):
        key = pipeline.deliver_script(self.request['script_sha256'])
        self.assertIn(self.request['script_sha256'], key)
        self.assertEqual(pipeline.deliver_script(self.request['script_sha256']), key)
        self.objects[key] = b'!' + self.objects[key][1:]
        with self.assertRaisesRegex(deploy.DeploymentError, 'checksum mismatch'):
            pipeline.deliver_script(self.request['script_sha256'])
        self.assertEqual(sum(call[1] == 'put-object' for call in self.calls), 3)

    def test_failed_tool_upload_never_falls_back_to_unconditional_write(self):
        self.aws.side_effect = pipeline.AWSFailure('AccessDenied')
        with self.assertRaises(pipeline.AWSFailure):
            pipeline.deliver_script(self.request['script_sha256'])
        self.assertEqual(self.aws.call_count, 1)

    def test_oversized_or_symlinked_tool_source_never_uploaded(self):
        (self.root / 'scripts').mkdir()
        path = self.root / 'scripts/deploy_ec2.py'
        with patch.object(pipeline, 'ROOT', self.root):
            path.write_bytes(b'x' * 262145)
            with self.assertRaisesRegex(deploy.DeploymentError, 'oversized'):
                pipeline.deliver_script(deploy.digest(path))
            path.unlink()
            path.symlink_to(self.checker)
            with self.assertRaisesRegex(deploy.DeploymentError, 'Unsafe'):
                pipeline.deliver_script(deploy.digest(path))
        self.aws.assert_not_called()

    def test_s3_size_limits_apply_before_get(self):
        self.aws.side_effect = None
        self.aws.return_value = {'ContentLength': 262145, 'ETag': '"abcd"'}
        with self.assertRaisesRegex(deploy.DeploymentError, 'size'):
            pipeline.get_object('tool', self.root / 'tool', 262144)
        self.assertEqual(self.aws.call_count, 1)

    def test_remote_command_is_bounded_supervised_and_requires_receipt(self):
        command = pipeline.remote_command(self.request)
        subprocess.run(['bash', '-n'], input=command, text=True, check=True, capture_output=True)
        tokens = shlex.split(command.splitlines()[1])
        for token in ('--wait', '--collect', '--unit=asaanpay-ci-deploy', '--property=RuntimeMaxSec=600',
                      '--property=TimeoutStopSec=30', '--property=KillMode=control-group'):
            self.assertIn(token, tokens)
        self.assertLess(len(command), 48000)
        self.assertIn('ASAANPAY_DEPLOY_OK', command.splitlines()[2])
        self.assertNotIn('rollback-safe-from', tokens)
        compile(tokens[tokens.index('-c') + 1], '<bootstrap-command>', 'exec')
        compile(shlex.split(command.splitlines()[2])[2], '<receipt-check>', 'exec')

    def test_remote_input_rejects_injection_wrong_origin_and_bad_order(self):
        for key, bad in [('nonce', "';touch /tmp/unsafe"), ('repository', 'fork/AsaanPay'),
                         ('branch', 'main'), ('workflow_id', True), ('run_number', -1), ('script_sha256', 'latest')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                pipeline.remote_command({**self.request, key: bad})
        with self.assertRaises(ValueError):
            pipeline.remote_command({**self.request, 'expected': {**self.expected, 'release_id': 'latest'}})

    def test_ssm_verified_success_only(self):
        report = {}
        with redirect_stdout(io.StringIO()):
            pipeline.send_and_wait(self.request, report)
        self.assertEqual(report, {'command_id': self.command_id, 'status': 'Success', 'health': 'PASS'})

    def test_ssm_terminal_failures_and_false_success(self):
        original = dict(self.invocation)
        for key, bad in [('Status', 'Failed'), ('Status', 'TimedOut'), ('Status', 'Cancelled'), ('Status', 'Cancelling'),
                         ('Status', 'Unknown'), ('ResponseCode', 1), ('StandardOutputContent', ''),
                         ('StandardOutputContent', 'ASAANPAY_DEPLOY_OK wrong-nonce'), ('InstanceId', 'i-other'),
                         ('DocumentName', 'other'), ('CommandId', 'other')]:
            self.invocation = {**original, key: bad}
            report = {}
            with self.subTest(key=key, bad=bad), redirect_stdout(io.StringIO()), self.assertRaises(deploy.DeploymentError):
                pipeline.send_and_wait(self.request, report)
            self.assertNotIn('health', report)

    def test_ssm_visibility_delay_is_bounded_and_retried(self):
        real = self.fake_aws
        first = True
        def aws(service, operation, *args, **kwargs):
            nonlocal first
            if operation == 'get-command-invocation' and first:
                first = False
                raise pipeline.AWSFailure('InvocationDoesNotExist')
            return real(service, operation, *args, **kwargs)
        self.aws.side_effect = aws
        report = {}
        with redirect_stdout(io.StringIO()):
            pipeline.send_and_wait(self.request, report, sleep=lambda _: None)
        self.assertEqual(report['health'], 'PASS')

    def test_ssm_poll_deadline_never_reports_late_success(self):
        ticks = iter([0, 0, 0, 781])
        report = {}
        with redirect_stdout(io.StringIO()), self.assertRaises(deploy.DeploymentError):
            pipeline.send_and_wait(self.request, report, clock=lambda: next(ticks), sleep=lambda _: None)
        self.assertNotIn('health', report)
        self.invocation['Status'] = 'InProgress'
        now = [0]
        def sleep(_):
            now[0] += 100
        with redirect_stdout(io.StringIO()), self.assertRaises(TimeoutError):
            pipeline.send_and_wait(self.request, {}, clock=lambda: now[0], sleep=sleep)

    def test_failed_ssm_send_is_not_retried(self):
        self.aws.side_effect = pipeline.AWSFailure('AccessDenied')
        with self.assertRaises(pipeline.AWSFailure):
            pipeline.send_and_wait(self.request, {})
        self.assertEqual(self.aws.call_count, 1)

    def test_uncertain_send_is_not_reported_as_not_sent_or_retried(self):
        self.aws.side_effect = subprocess.TimeoutExpired(['aws'], 30)
        report = {'command_id': 'Not sent', 'health': 'NOT VERIFIED'}
        with self.assertRaises(subprocess.TimeoutExpired):
            pipeline.send_and_wait(self.request, report)
        self.assertIn('submission attempted', report['command_id'])
        self.assertIn('unknown', report['status'])
        self.assertEqual(report['health'], 'NOT VERIFIED')
        self.assertEqual(self.aws.call_count, 1)

    def test_execute_checks_identity_publication_and_originating_run_before_ssm(self):
        with patch.object(pipeline, 'send_and_wait') as send:
            pipeline.execute(self.directory, self.request, {**self.env, 'PRODUCTION_DEPLOYMENT_ENABLED': 'true'}, {})
        send.assert_called_once()
        self.assertEqual(send.call_args.args[0]['expected'], self.expected)
        self.assertFalse(any('/git/ref/' in call.args[0] for call in self.github.call_args_list))
        self.assertEqual(self.calls[0][:2], ('sts', 'get-caller-identity'))

    def test_wrong_caller_or_instance_stops_before_ssm(self):
        real = self.fake_aws
        for target in ('get-caller-identity', 'describe-instance-information'):
            def bad(service, operation, *args, **kwargs):
                return {} if operation == target else real(service, operation, *args, **kwargs)
            self.aws.side_effect = bad
            with self.subTest(target=target), patch.object(pipeline, 'send_and_wait') as send, self.assertRaises(deploy.DeploymentError):
                pipeline.execute(self.directory, self.request, {**self.env, 'PRODUCTION_DEPLOYMENT_ENABLED': 'true'}, {})
            send.assert_not_called()

    def test_incomplete_publication_stops_before_tool_upload_and_ssm(self):
        self.objects.pop('releases/' + self.expected['release_id'] + '/SHA256SUMS')
        with patch.object(pipeline, 'send_and_wait') as send, self.assertRaises(pipeline.AWSFailure):
            pipeline.execute(self.directory, self.request, {**self.env, 'PRODUCTION_DEPLOYMENT_ENABLED': 'true'}, {})
        send.assert_not_called()
        self.assertFalse(any(call[1] == 'put-object' for call in self.calls))

    def test_head_change_during_validation_does_not_override_successful_run_provenance(self):
        def delivery(_):
            self.head['object']['sha'] = 'f' * 40
        with patch.object(pipeline, 'deliver_script', side_effect=delivery), patch.object(pipeline, 'send_and_wait') as send:
            pipeline.execute(self.directory, self.request, {**self.env, 'PRODUCTION_DEPLOYMENT_ENABLED': 'true'}, {})
        send.assert_called_once()

    def test_host_order_blocks_old_attempts_old_runs_and_crash_gap(self):
        current = {'release_id': self.expected['release_id']}
        order = {'workflow_id': 42, 'run_number': 7, 'run_attempt': 1, 'release_id': current['release_id']}
        host.check_order(self.request, current, order)
        host.check_order(self.request, None, {**order, 'run_number': 0, 'run_attempt': 0, 'release_id': None})
        for changed in ({'run_number': 8}, {'run_attempt': 2}, {'workflow_id': 43}, {'release_id': 'other'}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                host.check_order(self.request, current, {**order, **changed})

    def test_host_policy_must_be_explicitly_commissioned(self):
        host.check_policy(self.request, self.policy)
        for changes in ({'commissioned': False}, {'approval': {}}, {'contract': {**self.policy['contract'], 'no_database_changes': False}}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                host.check_policy(self.request, {**self.policy, **changes})

    def test_equal_rerun_newer_and_ambiguous_order_histories(self):
        current = {'release_id':self.expected['release_id']}
        order = {'workflow_id':42,'run_number':7,'run_attempt':1,'release_id':current['release_id']}
        host.check_order(self.request,current,order)  # Exact retry revalidates health.
        rerun = {**self.request,'expected':release.make_metadata('1.0.4','a'*40,'123','2')}
        host.check_order(rerun,current,order)
        newer = {**self.request,'run_number':8,'expected':release.make_metadata('1.0.4','b'*40,'124','1')}
        host.check_order(newer,current,order)
        for bad in ({**newer,'run_number':6}, {**newer,'run_number':7},
                    {**self.request,'run_number':8}, {**rerun,'expected':release.make_metadata('1.0.4','b'*40,'123','2')}):
            with self.subTest(bad=bad['run_number']), self.assertRaises(ValueError):
                host.check_order(bad,current,order)
        with self.assertRaisesRegex(ValueError,'Ambiguous'):
            host.check_order(newer,current,{**order,'run_attempt':2})

    def test_changed_checker_dependency_blocks_before_execution(self):
        self.policy['contract']['dependency_sha256']['scripts/release.py']='f'*64
        self.policy_path.write_text(json.dumps(self.policy))
        with self.assertRaisesRegex(deploy.DeploymentError,'dependency changed'):
            self.authorize()
        self.aws.assert_not_called()

    def test_aws_errors_do_not_echo_secrets(self):
        # Exercise the real adapter without ever invoking AWS.
        with patch.object(pipeline.subprocess, 'run', return_value=subprocess.CompletedProcess([], 254, '', 'secret (AccessDenied) secret')):
            with self.assertRaisesRegex(pipeline.AWSFailure, '^AWS request failed: AccessDenied$'):
                PipelineTests.real_aws('sts', 'get-caller-identity')

    real_aws = staticmethod(pipeline.aws)


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.content = b'# synthetic deployer fixture, never executed\n'
        self.checksum = host.digest(self.content)
        self.calls = []

    def aws(self, service, operation, *args):
        self.calls.append((service, operation, args))
        self.assertEqual(service, 's3api')
        self.assertIn('releases/deployment-tools/' + self.checksum + '/deploy_ec2.py', args)
        self.assertEqual(args[args.index('--expected-bucket-owner') + 1], deploy.ACCOUNT)
        if operation == 'get-object':
            self.assertIn('--if-match', args)
            self.assertIn('--range', args)
            Path(args[-1]).write_bytes(self.content)
        else:
            self.assertEqual(operation, 'head-object')
        return {'ContentLength': len(self.content), 'ETag': '"abcd"'}

    def test_install_verified_bytes_and_reuse_only_matching_immutable_file(self):
        # Local test owner replaces root ownership only; check permissions below.
        with patch.object(host, 'BASE', self.base), patch.object(host, 'secure'), patch.object(host, 'aws', side_effect=self.aws):
            path = host.install(self.checksum)
            self.assertEqual(path.read_bytes(), self.content)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(host.install(self.checksum), path)
            self.assertEqual(len(self.calls), 2)
            path.write_bytes(b'tampered')
            with self.assertRaisesRegex(ValueError, 'Installed deployer checksum'):
                host.install(self.checksum)
            self.assertEqual(path.read_bytes(), b'tampered')

    def test_corrupt_tool_is_never_installed(self):
        self.content = b'corrupted response'
        with patch.object(host, 'BASE', self.base), patch.object(host, 'secure'), patch.object(host, 'aws', side_effect=self.aws), \
                self.assertRaisesRegex(ValueError, 'Downloaded deployer checksum'):
            host.install(self.checksum)
        self.assertEqual(list((self.base / 'deployers').iterdir()), [])

    def test_oversized_tool_rejected_before_download(self):
        with patch.object(host, 'BASE', self.base), patch.object(host, 'secure'), \
                patch.object(host, 'aws', return_value={'ContentLength': 262145, 'ETag': '"abcd"'}) as aws, \
                self.assertRaisesRegex(ValueError, 'Unsafe deployer object'):
            host.install(self.checksum)
        self.assertEqual(aws.call_count, 1)

    def test_root_ownership_symlinks_and_permissions(self):
        path = Mock()
        path.lstat.return_value = SimpleNamespace(st_uid=0, st_mode=stat.S_IFREG | 0o600)
        host.secure(path, mode=0o600)
        for uid, mode in ((1000, stat.S_IFREG | 0o600), (0, stat.S_IFREG | 0o666),
                          (0, stat.S_IFLNK | 0o600), (0, stat.S_IFDIR | 0o600), (0, stat.S_IFREG | 0o644)):
            path.lstat.return_value = SimpleNamespace(st_uid=uid, st_mode=mode)
            with self.subTest(uid=uid, mode=mode), self.assertRaises(ValueError):
                host.secure(path, mode=0o600)

    def test_instance_identity_and_role_are_verified_without_static_credentials(self):
        identity = {'accountId': host.ACCOUNT, 'region': 'us-east-1', 'instanceId': host.INSTANCE}
        caller = {'Account': host.ACCOUNT, 'Arn': 'arn:aws:sts::' + host.ACCOUNT + ':assumed-role/AsaanPay-EC2-Role/' + host.INSTANCE}
        def opened(request, **kwargs):
            if request.full_url.endswith('/api/token'):
                self.assertEqual(request.method, 'PUT')
                return nullcontext(io.BytesIO(b'fixture-token'))
            self.assertEqual(request.full_url, 'http://169.254.169.254/latest/dynamic/instance-identity/document')
            return nullcontext(io.BytesIO(json.dumps(identity).encode()))
        with patch.object(host, 'build_opener') as opener, patch.object(host, 'aws', return_value=caller):
            opener.return_value.open.side_effect = opened
            host.verify_instance()
            for key, bad in (('accountId', 'other'), ('region', 'us-west-2'), ('instanceId', 'i-other')):
                saved = identity[key]
                identity[key] = bad
                with self.subTest(key=key), self.assertRaises(ValueError):
                    host.verify_instance()
                identity[key] = saved
            caller['Arn'] += '-wrong-session'
            with self.assertRaises(ValueError):
                host.verify_instance()
        self.assertNotIn('AWS_ACCESS_KEY_ID', host.ENV)
        self.assertEqual(host.ENV['AWS_SHARED_CREDENTIALS_FILE'], '/dev/null')

    def test_host_reads_are_bounded(self):
        path = self.base / 'metadata'
        path.write_bytes(b'x' * 100)
        with patch.object(host, 'secure'), self.assertRaisesRegex(ValueError, 'Excessive'):
            host.read(path, 10)


class HostIntegrationTests(unittest.TestCase):
    """Exercise the wrapper with the real M7E.5A state machine; Docker/AWS mocked."""
    def setUp(self):
        helper = deployment_fixtures.DeploymentTests('test_valid_package_archive_and_shared_validation_parity')
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        self.helper = helper
        self.subject = helper.subject
        self.request = {'expected': deploy.validate_manifest(helper.manifest), 'image_id': helper.manifest['image_id'],
                        'marker_sha256': deploy.digest(helper.source / 'SHA256SUMS'),
                        'facts': host.contract_facts(helper.source, compose_config(helper.source, helper.manifest)),
                        'workflow_id': 42, 'run_number': 7}
        self.volume = {'Name': deploy.VOLUME, 'Driver': 'local', 'Options': None, 'Mountpoint': '/fixture', 'CreatedAt': 'fixture'}
        self.baseline = {'database_container_id': 'existing-db', 'volume': self.volume, 'network': {'Id': 'network-id'},
                         'order': {'workflow_id': 42, 'run_number': 0, 'run_attempt': 0, 'release_id': None}}
        self.policy = demo_fixtures.policy(self.request['facts'], self.request['expected']['release_id'])
        self.request['policy_sha256'] = host.digest(json.dumps(self.policy).encode())
        self.request['evidence'] = demo_fixtures.evidence(self.request, self.policy)
        self.baseline.update(schema_version=1,policy_sha256=self.request['policy_sha256'],
                             baseline_release_id=self.policy['contract']['baseline_release_id'],
                             approval={'reviewer':'TEST ONLY','review_reference':'unit-fixture'},live_schema=demo_fixtures.schema())
        with patch.object(host, 'BASE', helper.base):
            wrapper = host.ordered_deployer(deploy, self.request, self.policy, self.baseline)
        self.subject.__class__ = type(wrapper)
        p = patch.object(host, 'secure')
        p.start()
        self.addCleanup(p.stop)
        def docker(*args, **kwargs):
            if args[:2] == ('exec', '--interactive'):
                self.assertEqual(args[2], 'existing-db')
                self.assertIn('-h /var/run/postgresql -p 5432',args[-1])
                self.assertEqual(kwargs['input_text'], host.SCHEMA_SQL)
                self.assertEqual(kwargs['timeout'], 15)
                return json.dumps(self.live_schema)
            if args[:2] == ('volume', 'inspect'):
                return json.dumps([self.volume])
            return helper.fake_docker(*args, **kwargs)
        helper.docker_mock.side_effect = docker
        self.live_schema = demo_fixtures.schema()

    def test_first_adoption_and_subsequent_policy_authorized_update(self):
        self.helper.call()
        first = self.helper.state()['current']['release_id']
        order = json.loads((self.helper.base / 'deployment-order.json').read_text())
        self.assertEqual(order['release_id'], first)
        directory, manifest = self.helper.next_release()
        self.request.update(expected=deploy.validate_manifest(manifest), image_id=manifest['image_id'],
                            marker_sha256=deploy.digest(directory / 'SHA256SUMS'))
        self.request['evidence'] = demo_fixtures.evidence(self.request, self.policy)
        self.helper.call(manifest['release_id'])
        self.assertEqual(self.helper.state()['current']['release_id'], manifest['release_id'])
        self.assertIsNone(self.helper.state()['in_progress'])

    def test_failed_update_retains_interrupted_state_and_order(self):
        self.helper.call()
        first = self.helper.state()['current']['release_id']
        directory, manifest = self.helper.next_release()
        self.request.update(expected=deploy.validate_manifest(manifest), image_id=manifest['image_id'],
                            marker_sha256=deploy.digest(directory / 'SHA256SUMS'))
        self.request['evidence'] = demo_fixtures.evidence(self.request, self.policy)
        self.helper.fail_start = True
        with self.assertRaises(deploy.DeploymentError):
            self.helper.call(manifest['release_id'])
        self.assertEqual(self.helper.state()['in_progress']['release_id'], manifest['release_id'])
        self.assertEqual(json.loads((self.helper.base / 'deployment-order.json').read_text())['release_id'], first)
        before = len(self.helper.commands)
        with self.assertRaisesRegex(ValueError, 'Interrupted'):
            self.helper.call(manifest['release_id'])
        self.assertEqual(len(self.helper.commands), before)

    def test_changed_database_volume_or_contract_blocks_before_image_load(self):
        for name, value in [('database_container_id', 'other-db'), ('volume', {}), ('order', {'workflow_id': 43})]:
            saved = self.baseline[name]
            self.baseline[name] = value
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.helper.call()
            self.baseline[name] = saved
        self.request['facts']['schema_sha256'] = 'f' * 64
        with self.assertRaisesRegex(ValueError, 'approved host contract'):
            self.helper.call()
        self.assertFalse(any(command[0] in ('load', 'compose') for command in self.helper.commands))
        self.assertFalse(self.subject.state_file.exists())

    def test_state_order_gap_is_not_automatically_reconciled(self):
        self.helper.call()
        (self.helper.base / 'deployment-order.json').unlink()
        with self.assertRaisesRegex(ValueError, 'state/order mismatch'):
            self.helper.call()

    def test_live_schema_difference_blocks_before_image_load(self):
        self.live_schema['columns'][0]['not_null'] = False
        with self.assertRaisesRegex(ValueError, 'Live database differs'):
            self.helper.call()
        self.assertFalse(any(command[0] in ('load','compose') for command in self.helper.commands))
        self.assertFalse(self.subject.state_file.exists())

    def test_unapproved_baseline_or_inaccessible_database_blocks_before_image_load(self):
        self.baseline['live_schema']['columns'][0]['not_null']=False
        with self.assertRaisesRegex(ValueError,'baseline differs'):
            self.helper.call()
        self.baseline['live_schema']=demo_fixtures.schema()
        original=self.helper.docker_mock.side_effect
        def inaccessible(*args,**kwargs):
            if args[:2]==('exec','--interactive'):
                raise deploy.DeploymentError('Read-only database probe timed out')
            return original(*args,**kwargs)
        self.helper.docker_mock.side_effect=inaccessible
        with self.assertRaisesRegex(deploy.DeploymentError,'timed out'):
            self.helper.call()
        self.assertFalse(any(command[0] in ('load','compose') for command in self.helper.commands))

    def test_missing_approval_or_evidence_blocks_wrapper(self):
        for value, key in ((self.policy,'commissioned'),(self.baseline,'approval'),(self.request,'evidence')):
            original = value[key]
            value[key] = False if key=='commissioned' else {}
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.helper.call()
            value[key] = original
        self.assertEqual(self.helper.commands, [])

    def test_schema_is_rechecked_after_image_load_before_api_replacement(self):
        real = self.helper.docker_mock.side_effect
        def docker(*args, **kwargs):
            result = real(*args, **kwargs)
            if args[0] == 'load':
                self.live_schema['indexes'].append({'name':'unexpected'})
            return result
        self.helper.docker_mock.side_effect = docker
        with self.assertRaises(deploy.DeploymentError):
            self.helper.call()
        self.assertFalse(any(command[0]=='compose' for command in self.helper.commands))
        self.assertIsNotNone(self.helper.state()['in_progress'])



if __name__ == '__main__':
    unittest.main()
