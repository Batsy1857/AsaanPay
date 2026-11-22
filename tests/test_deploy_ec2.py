"""Standalone deployer safety tests; no AWS requests or containers are started."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import hashlib
import io
import json
import os
import signal
import sys
import time
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from scripts import deploy_ec2 as deploy
from scripts import release

DB_IMAGE = 'postgres:17-alpine@sha256:' + 'd' * 64


def fixture(directory, attempt='1', architecture='amd64'):
    directory.mkdir(parents=True)
    (directory / 'db').mkdir()
    metadata = release.make_metadata('1.0.4', 'a' * 40, '123', attempt)
    labels = dict(zip(('org.opencontainers.image.version', 'org.opencontainers.image.revision',
                       'io.asaanpay.release-id', 'io.asaanpay.run-id', 'io.asaanpay.run-attempt'),
                      (metadata[k] for k in ('version', 'commit', 'release_id', 'run_id', 'run_attempt'))))
    config = {'os': 'linux', 'architecture': architecture, 'rootfs': {'type': 'layers', 'diff_ids': ['sha256:' + hashlib.sha256(b'').hexdigest()]}, 'config': {'User': '10001:10001', 'Labels': labels}}
    data = json.dumps(config, sort_keys=True).encode()
    image_id = 'sha256:' + hashlib.sha256(data).hexdigest()
    image = 'asaanpay:' + metadata['release_id']
    with tarfile.open(directory / 'image.tar.gz', 'w:gz') as archive:
        for name, content in (
            ('config.json', data), ('layer.tar', b''),
            ('manifest.json', json.dumps([{'Config': 'config.json', 'RepoTags': [image], 'Layers': ['layer.tar']}]).encode()),
        ):
            entry = tarfile.TarInfo(name)
            entry.size = len(content)
            archive.addfile(entry, io.BytesIO(content))
    (directory / 'db/schema.sql').write_text('CREATE TABLE fixture (id INTEGER);\n')
    manifest = {**metadata, 'schema_version': 1, 'platform': 'linux/amd64', 'image': image, 'image_id': image_id,
                'archive': 'image.tar.gz', 'archive_sha256': deploy.digest(directory / 'image.tar.gz'),
                'local_validation': False, 'source_dirty': False}
    (directory / 'compose.prod.yaml').write_text(json.dumps(compose_config(directory, manifest)))
    rewrite(directory, manifest)
    return manifest


def rewrite(directory, manifest):
    (directory / 'manifest.json').write_text(json.dumps(manifest))
    (directory / 'SHA256SUMS').write_text(''.join(f'{deploy.digest(directory / name)}  {name}\n' for name in deploy.PACKAGE_FILES))


def compose_config(directory, manifest):
    db_env = {'POSTGRES_USER': 'test', 'POSTGRES_DB': 'test', 'POSTGRES_PASSWORD': 'do-not-log-this-secret'}
    return {'name': 'asaanpay', 'volumes': {'postgres_data': {'name': deploy.VOLUME}}, 'services': {
        'api': {'image': manifest['image'], 'environment': {**db_env, 'POSTGRES_HOST': 'db', 'POSTGRES_PORT': '5432'},
                'ports': [{'target': 8000, 'published': '8000', 'host_ip': '127.0.0.1', 'protocol': 'tcp'}]},
        'db': {'image': DB_IMAGE, 'environment': db_env, 'volumes': [
            {'type': 'volume', 'source': 'postgres_data', 'target': '/var/lib/postgresql/data'},
            {'type': 'bind', 'source': str((directory / 'db/schema.sql').resolve()),
             'target': '/docker-entrypoint-initdb.d/001-schema.sql', 'read_only': True}]}}}


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.base = self.root / 'production'
        self.base.mkdir()
        (self.base / 'releases').mkdir()
        self.source = self.root / 's3'
        self.manifest = fixture(self.source)
        self.release_id = self.manifest['release_id']
        self.subject = deploy.Deployer(self.base)
        self.commands = []
        self.sources = {self.release_id: self.source}
        self.manifests = {self.manifest['image']: self.manifest}
        self.fail_download = self.fail_load = self.fail_start = False
        for name, target in (
            ('preflight', None), ('run', self.fake_run), ('docker', self.fake_docker),
            ('configuration', lambda directory, manifest: compose_config(directory, manifest)),
            ('database', lambda config: 'existing-db'), ('health', None),
            ('network', lambda db_id: 'network-id'),
            ('inspect_archive', lambda directory, manifest: deploy.validate_archive(directory / 'image.tar.gz', manifest)),
        ):
            p = patch.object(self.subject, name, side_effect=target)
            mocked = p.start()
            setattr(self, name + '_mock', mocked)
            self.addCleanup(p.stop)
        # Temp files belong to the local test user; separately test root-owner validation.
        p = patch.object(deploy, 'disk_headroom')
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(deploy, 'secure_path')
        p.start()
        self.addCleanup(p.stop)

    def fake_run(self, *args, **kwargs):
        self.commands.append(args)
        self.assertEqual(args[:2], ('aws', 's3api'))
        self.assertIn(args[2], ('get-object', 'head-object'))
        self.assertEqual(args[args.index('--bucket') + 1], 'asaanpay-releases')
        self.assertIn(deploy.ACCOUNT, args)
        if self.fail_download:
            raise deploy.DeploymentError('AWS download failed', 4)
        key = args[args.index('--key') + 1]
        prefix, release_id, name = key.split('/', 2)
        self.assertEqual(prefix, 'releases')
        self.assertIn(name, deploy.FILES)
        source = self.sources[release_id] / name
        response = {'ContentLength': source.stat().st_size, 'ETag': '"' + deploy.digest(source) + '"'}
        if args[2] == 'get-object':
            self.assertEqual(args[args.index('--if-match') + 1], response['ETag'])
            self.assertEqual(args[args.index('--range') + 1], 'bytes=0-' + str(source.stat().st_size - 1))
            shutil.copyfile(source, args[-1])
        return json.dumps(response)

    def fake_docker(self, *args, **kwargs):
        self.commands.append(args)
        if args[0] == 'info':
            return json.dumps(str(self.root))
        if args[0] == 'load':
            if self.fail_load:
                raise deploy.DeploymentError('Docker load failed', 4)
            return ''
        if args[:2] == ('image', 'inspect'):
            manifest = self.manifests[args[2]]
            with tarfile.open(self.sources[manifest['release_id']] / 'image.tar.gz', 'r:gz') as archive:
                config = json.load(archive.extractfile('config.json'))
            return json.dumps([{'Id': manifest['image_id'], 'Os': config['os'], 'Architecture': config['architecture'], 'Config': config['config']}])
        if args[0] == 'compose' and 'up' in args:
            self.assertEqual(args[-1], 'api')
            self.assertIn('--no-deps', args)
            self.assertIn('--no-build', args)
            self.assertEqual(args[args.index('--pull') + 1], 'never')
            self.assertEqual(args[args.index('--project-name') + 1], 'asaanpay')
            self.assertEqual(args[args.index('--env-file') + 1], '/dev/null')
            projected = json.loads(kwargs['input_text'])
            self.assertEqual(set(projected['services']), {'api'})
            self.assertEqual(projected['networks'], {'default': {'name': 'network-id', 'external': True}})
            self.assertNotIn('volumes', projected)
            if self.fail_start:
                raise deploy.DeploymentError('Compose startup failed', 4)
            return ''
        self.fail('Unexpected Docker command: ' + repr(args))

    def call(self, release_id=None, **kwargs):
        with redirect_stdout(io.StringIO()):
            self.subject.deploy(release_id or self.release_id, **kwargs)

    def state(self):
        return json.loads(self.subject.state_file.read_text())

    def next_release(self):
        directory = self.root / 's3-next'
        manifest = fixture(directory, '2')
        self.sources[manifest['release_id']] = directory
        self.manifests[manifest['image']] = manifest
        return directory, manifest

    def test_valid_package_archive_and_shared_validation_parity(self):
        self.assertEqual(deploy.validate_package(self.source, self.release_id), self.manifest)
        # release.py targets CI's Python 3.13 and uses hashlib.file_digest. Keep
        # this parity test runnable under the standalone deployer's Python 3.9.
        with patch.object(release, 'digest', deploy.digest):
            self.assertEqual(release.check_checksums(self.source), deploy.check_checksums(self.source))
        deploy.validate_archive(self.source / 'image.tar.gz', self.manifest)
        config = compose_config(self.source, self.manifest)
        deploy.validate_compose(config, self.source, self.manifest)
        with patch.object(release, 'docker', return_value=json.dumps(config)):
            self.assertEqual(release.validate_deployment(self.source, self.manifest), DB_IMAGE)

    def test_malformed_release_id_before_any_operations(self):
        for bad in ('', '../escape', self.release_id + '/file', '1.0.4-gabc-r1-a1', self.release_id.replace('-r123', '-r0')):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.subject.deploy(bad)
        self.preflight_mock.assert_not_called()
        self.assertEqual(self.commands, [])

    def test_missing_corrupt_unexpected_symlink_and_unsafe_entries(self):
        for name in deploy.FILES:
            path = self.source / name
            data = path.read_bytes()
            for value in (None, data + b'corruption'):
                path.unlink() if value is None else path.write_bytes(value)
                with self.subTest(name=name), self.assertRaises(ValueError):
                    deploy.validate_package(self.source, self.release_id)
                path.write_bytes(data)
            path.unlink()
            target = self.root / 'outside'
            target.write_bytes(data)
            path.symlink_to(target)
            with self.assertRaises(ValueError):
                deploy.validate_package(self.source, self.release_id)
            path.unlink()
            path.write_bytes(data)
        (self.source / 'extra').write_text('unexpected')
        with self.assertRaises(ValueError):
            deploy.validate_package(self.source, self.release_id)
        (self.source / 'extra').unlink()
        (self.source / 'SHA256SUMS').write_text('a' * 64 + '  ../outside\n')
        with self.assertRaises(ValueError):
            deploy.validate_package(self.source, self.release_id)

    def test_manifest_dirty_local_and_architecture_rejected(self):
        for field, value in (('local_validation', True), ('source_dirty', True), ('platform', 'linux/arm64'),
                             ('image', 'asaanpay:latest'), ('archive_sha256', '0' * 64), ('commit', 'c' * 40)):
            rewrite(self.source, {**self.manifest, field: value})
            with self.subTest(field=field), self.assertRaises(ValueError):
                deploy.validate_package(self.source, self.release_id)
        rewrite(self.source, self.manifest)
        with self.assertRaises(ValueError):
            deploy.validate_package(self.source, self.release_id.replace('-a1', '-a2'))

    def test_unsafe_compose_rejected(self):
        mutations = [(('name',), 'other'), (('services', 'db', 'ports'), [{'target': 5432}]),
                     (('services', 'api', 'ports', 0, 'host_ip'), '0.0.0.0'),
                     (('services', 'api', 'image'), 'other'), (('services', 'db', 'image'), 'postgres:17-alpine'),
                     (('volumes', 'postgres_data', 'name'), 'other'),
                     (('volumes', 'postgres_data', 'driver_opts'), {'device': '/', 'o': 'bind'})]
        for service in ('api', 'db'):
            for field, value in (('privileged', True), ('network_mode', 'host'), ('pid', 'host'), ('ipc', 'host'),
                                 ('cap_add', ['SYS_ADMIN']), ('devices', ['/dev/sda']), ('use_api_socket', True)):
                mutations.append((('services', service, field), value))
        for path, value in mutations:
            config = compose_config(self.source, self.manifest)
            node = config
            for key in path[:-1]:
                node = node[key]
            node[path[-1]] = value
            with self.subTest(path=path), self.assertRaises(ValueError):
                deploy.validate_compose(config, self.source, self.manifest)
            # Deployment is deliberately stricter only about the database digest here.
            if path != ('services', 'db', 'image'):
                with patch.object(release, 'docker', return_value=json.dumps(config)), self.assertRaises(ValueError):
                    release.validate_deployment(self.source, self.manifest)

    def test_wrong_loaded_image_id_architecture_labels(self):
        original = json.loads(self.fake_docker('image', 'inspect', self.manifest['image']))[0]
        for field, value in (('Id', 'sha256:' + '0' * 64), ('Architecture', 'arm64'), ('Config', {'User': '0', 'Labels': {}})):
            with patch.object(self.subject, 'docker', return_value=json.dumps([{**original, field: value}])), self.assertRaises(ValueError):
                self.subject.image(self.manifest)

    def test_archive_identity_and_unsafe_tar_before_load(self):
        with self.assertRaises(ValueError):
            deploy.validate_archive(self.source / 'image.tar.gz', {**self.manifest, 'image_id': 'sha256:' + '0' * 64})
        for name, kind in (('../outside', tarfile.REGTYPE), ('link', tarfile.SYMTYPE)):
            with tarfile.open(self.root / 'bad.tar.gz', 'w:gz') as archive:
                member = tarfile.TarInfo(name)
                member.type = kind
                member.linkname = '/etc/passwd'
                archive.addfile(member, io.BytesIO(b''))
            with self.assertRaises(ValueError):
                deploy.validate_archive(self.root / 'bad.tar.gz', self.manifest)

    def test_wrong_archive_architecture_rejected_before_load(self):
        bad_source = self.root / 'wrong-architecture'
        bad = fixture(bad_source, '3', architecture='arm64')
        self.sources[bad['release_id']] = bad_source
        with self.assertRaisesRegex(ValueError, 'platform'):
            self.call(bad['release_id'])
        self.assertFalse(any(command[0] == 'load' or 'up' in command for command in self.commands))

    def test_failed_download_and_load_leave_running_state_untouched(self):
        for failure in ('fail_download', 'fail_load'):
            setattr(self, failure, True)
            with self.subTest(failure=failure), self.assertRaises(deploy.DeploymentError):
                self.call()
            setattr(self, failure, False)
            if failure == 'fail_load':
                self.assertIsNotNone(self.state()['in_progress'])
                self.assertIsNone(self.state()['current'])
            else:
                self.assertFalse(self.subject.state_file.exists())
            self.assertFalse(any('up' in command for command in self.commands))
            self.assertFalse(list(self.subject.releases.glob('.staging-*')))

    def test_invalid_compose_fails_before_docker_load(self):
        self.configuration_mock.side_effect = deploy.DeploymentError('unsafe Compose')
        with self.assertRaises(deploy.DeploymentError):
            self.call()
        self.assertFalse(any(command[0] == 'load' for command in self.commands))
        self.assertFalse(self.subject.state_file.exists())

    def test_success_and_repeat_do_not_restart_or_redownload(self):
        self.call()
        state = self.state()
        self.assertEqual(state['current']['release_id'], self.release_id)
        self.assertIsNone(state['in_progress'])
        self.assertEqual(len([command for command in self.commands if command[:3] == ('aws', 's3api', 'get-object')]), 5)
        self.assertEqual(len([command for command in self.commands if 'up' in command]), 1)
        self.commands.clear()
        self.call()
        self.assertEqual(self.state(), state)
        self.assertFalse(any(command[0] in ('aws', 'load') or 'up' in command for command in self.commands))
        self.assertEqual(self.health_mock.call_count, 2)

    def test_failed_start_and_health_never_record_success(self):
        self.fail_start = True
        with self.assertRaises(deploy.DeploymentError) as error:
            self.call()
        self.assertEqual(error.exception.code, 5)
        self.assertIsNone(self.state()['current'])
        self.assertEqual(self.state()['in_progress']['release_id'], self.release_id)
        self.fail_start = False
        self.health_mock.side_effect = deploy.DeploymentError('API/database health failed')
        with self.assertRaises(deploy.DeploymentError):
            self.call(resume_interrupted=True, operations_stopped=True)
        self.assertIsNone(self.state()['current'])

    def test_concurrent_deployment_is_rejected(self):
        with deploy.deployment_lock(self.base):
            with self.assertRaises(deploy.DeploymentError) as error:
                self.call()
            self.assertEqual(error.exception.code, 3)
        self.assertEqual(self.commands, [])

    def test_failed_update_never_automatically_rolls_back(self):
        self.call()
        old = self.state()['current']
        _, candidate = self.next_release()
        self.health_mock.side_effect = deploy.DeploymentError('unhealthy')
        with self.assertRaises(deploy.DeploymentError) as error:
            self.call(candidate['release_id'], rollback_safe_from=self.release_id)
        self.assertEqual(error.exception.code, 5)
        self.assertEqual(self.state()['current'], old)
        self.assertEqual(self.state()['in_progress']['release_id'], candidate['release_id'])
        self.assertEqual(len([c for c in self.commands if 'up' in c]), 2)

    def interrupted_candidate(self, changed_schema=False):
        self.call()
        source, candidate = self.next_release()
        if changed_schema:
            (source / 'db/schema.sql').write_text('incompatible schema')
            rewrite(source, candidate)
        # Simulate an interruption left by the earlier implementation, including
        # one whose candidate schema differs from the recorded current release.
        self.subject.stage(candidate['release_id'])
        state = self.state()
        state['in_progress'] = {'release_id': candidate['release_id'], 'previous_release_id': self.release_id}
        deploy.atomic_state(self.subject.state_file, state)
        self.commands.clear()
        return candidate

    def test_confirmed_restoration_bypass_is_rejected_even_for_matching_sql(self):
        candidate = self.interrupted_candidate()
        for approval in (None, self.release_id):
            with self.assertRaisesRegex(deploy.DeploymentError, 'Restoration requires'):
                self.call(resume_interrupted=True, operations_stopped=True, rollback_safe_from=approval)
        self.assertEqual(self.commands, [])
        self.assertEqual(self.state()['in_progress']['release_id'], candidate['release_id'])

    def test_restoration_requires_matching_schema_and_explicit_live_approval(self):
        candidate = self.interrupted_candidate(changed_schema=True)
        with self.assertRaisesRegex(deploy.DeploymentError, 'Schema/database change'):
            self.call(resume_interrupted=True, operations_stopped=True, rollback_safe_from=candidate['release_id'])
        self.assertFalse(any(c[0] == 'load' or 'up' in c for c in self.commands))
        self.assertIsNotNone(self.state()['in_progress'])

    def test_explicit_restoration_verifies_before_clearing_journal(self):
        candidate = self.interrupted_candidate()
        self.call(resume_interrupted=True, operations_stopped=True, rollback_safe_from=candidate['release_id'])
        self.assertIsNone(self.state()['in_progress'])
        self.assertEqual(self.state()['current']['release_id'], self.release_id)

    def test_candidate_resume_requires_operations_stopped_not_just_resume(self):
        candidate = self.interrupted_candidate()
        with self.assertRaisesRegex(deploy.DeploymentError, 'operations-stopped'):
            self.call(candidate['release_id'], resume_interrupted=True)
        self.assertEqual(self.commands, [])
        self.call(candidate['release_id'], resume_interrupted=True, operations_stopped=True)
        self.assertEqual(self.state()['current']['release_id'], candidate['release_id'])

    def test_live_old_process_group_blocks_recovery_even_when_acknowledged(self):
        self.interrupted_candidate()
        state = self.state()
        state['in_progress']['operation_pgid'] = os.getpgrp()
        deploy.atomic_state(self.subject.state_file, state)
        with self.assertRaisesRegex(deploy.DeploymentError, 'still present'):
            self.call(resume_interrupted=True, operations_stopped=True)
        self.assertEqual(self.commands, [])

    def test_schema_change_blocks_transition_before_containers_change(self):
        self.call()
        source, candidate = self.next_release()
        (source / 'db/schema.sql').write_text('incompatible schema')
        rewrite(source, candidate)
        self.commands.clear()
        with self.assertRaises(deploy.DeploymentError):
            self.call(candidate['release_id'], rollback_safe_from=self.release_id)
        self.assertFalse(any('up' in c or c[0] == 'load' for c in self.commands))

    def test_historical_or_new_transition_requires_operational_approval(self):
        self.call()
        _, candidate = self.next_release()
        self.commands.clear()
        with self.assertRaisesRegex(deploy.DeploymentError, 'explicit live-database'):
            self.call(candidate['release_id'])
        self.assertFalse(any('up' in c or c[0] == 'load' for c in self.commands))

    def test_interrupt_requires_explicit_resume_and_revalidation(self):
        self.health_mock.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.call()
        self.assertIsNone(self.state()['current'])
        self.commands.clear()
        with self.assertRaises(deploy.DeploymentError):
            self.call()
        self.assertEqual(self.commands, [])
        self.health_mock.side_effect = None
        self.call(resume_interrupted=True, operations_stopped=True)
        self.assertEqual(self.state()['current']['release_id'], self.release_id)
        self.assertIsNone(self.state()['in_progress'])

    def test_abandoned_staging_not_reused_and_corrupt_cache_not_overwritten(self):
        abandoned = self.subject.releases / '.staging-interrupted'
        abandoned.mkdir()
        (abandoned / 'partial').write_text('partial')
        self.call()
        self.assertTrue((abandoned / 'partial').exists())
        cached = self.subject.releases / self.release_id / 'db/schema.sql'
        cached.write_text('corrupt')
        self.commands.clear()
        with self.assertRaises(ValueError):
            self.call()
        self.assertEqual(cached.read_text(), 'corrupt')
        self.assertEqual(self.commands, [])

    def test_never_deletes_or_starts_database_or_builds_or_pulls(self):
        self.call()
        for command in self.commands:
            self.assertFalse(set(command) & {'down', 'rm', 'prune', 'build', 'pull', 'create', 'exec'})
            if 'up' in command:
                self.assertEqual(command[-1], 'api')
                self.assertIn('--no-deps', command)

    def test_unhealthy_same_release_is_repaired_and_reverified(self):
        self.call()
        self.commands.clear()
        self.health_mock.side_effect = [deploy.DeploymentError('unhealthy'), None]
        self.call()
        self.assertEqual(sum('up' in c for c in self.commands), 1)
        self.assertIsNone(self.state()['in_progress'])
        self.assertEqual(self.state()['current']['release_id'], self.release_id)

    def test_live_network_failure_precedes_image_load(self):
        self.network_mock.side_effect = deploy.DeploymentError('network drift')
        with self.assertRaisesRegex(deploy.DeploymentError, 'network drift'):
            self.call()
        self.assertFalse(any(c[0] == 'load' or 'up' in c for c in self.commands))
        self.assertFalse(self.subject.state_file.exists())

    def test_load_disk_headroom_failure_preserves_running_installation(self):
        self.subject.stage(self.release_id)
        self.commands.clear()
        with patch.object(deploy, 'disk_headroom', side_effect=deploy.DeploymentError('low disk')):
            with self.assertRaisesRegex(deploy.DeploymentError, 'low disk'):
                self.call()
        self.assertFalse(any(c[0] == 'load' or 'up' in c for c in self.commands))
        self.assertFalse(self.subject.state_file.exists())

    def test_conditional_download_failure_does_not_retry_unconditionally(self):
        def transfer(*args, **kwargs):
            if args[2] == 'get-object':
                self.assertIn('--if-match', args)
                raise deploy.DeploymentError('PreconditionFailed')
            return self.fake_run(*args, **kwargs)
        self.run_mock.side_effect = transfer
        with self.assertRaisesRegex(deploy.DeploymentError, 'PreconditionFailed'):
            self.call()
        self.assertEqual(sum(c.args[2] == 'get-object' for c in self.run_mock.call_args_list), 1)
        self.assertFalse(list(self.subject.releases.glob('.staging-*')))
        self.assertFalse(self.subject.state_file.exists())


class RuntimeSafetyTests(unittest.TestCase):
    def test_no_static_credentials_or_compose_overrides_inherited(self):
        with patch.dict(os.environ, {'AWS_ACCESS_KEY_ID': 'secret', 'AWS_PROFILE': 'admin',
                                     'POSTGRES_PASSWORD': 'secret', 'COMPOSE_FILE': '/unsafe'}):
            environment = deploy.Deployer().environment
        for name in ('AWS_ACCESS_KEY_ID', 'AWS_PROFILE', 'POSTGRES_PASSWORD', 'COMPOSE_FILE'):
            self.assertNotIn(name, environment)
        self.assertEqual(environment['AWS_SHARED_CREDENTIALS_FILE'], '/dev/null')
        self.assertEqual(environment['AWS_CONFIG_FILE'], '/dev/null')

    def test_command_errors_never_expose_secret_stdout_stderr_or_argv(self):
        subject = deploy.Deployer()
        with self.assertRaises(deploy.DeploymentError) as error:
            subject.run(sys.executable, '-c', 'import sys; print("do-not-log-this-secret"); sys.exit(1)')
        self.assertNotIn('do-not-log', str(error.exception))

    def test_cli_redacts_unexpected_errors_and_returns_meaningful_exit_codes(self):
        for error, expected_code in ((deploy.DeploymentError('safe deployment error', 7), 7),
                                     (ValueError('do-not-log-this-secret'), 2), (deploy.Cancelled(), 130)):
            with patch.object(deploy.Deployer, 'deploy', side_effect=error), redirect_stderr(io.StringIO()) as output:
                self.assertEqual(deploy.main(['fixture']), expected_code)
            self.assertNotIn('do-not-log', output.getvalue())
            self.assertNotIn('Traceback', output.getvalue())

    def test_environment_file_permissions_and_symlinks_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'production.env'
            path.write_text('not-logged')
            path.chmod(0o644)
            with self.assertRaises(deploy.DeploymentError):
                deploy.secure_path(path, mode=0o600)
            path.unlink()
            path.symlink_to('/etc/passwd')
            with self.assertRaises(deploy.DeploymentError):
                deploy.secure_path(path, mode=0o600)

    def test_actual_compose_commands_use_dummy_then_production_env_without_logging(self):
        subject = deploy.Deployer()
        directory = Path('/opt/asaanpay/releases/fixture')
        manifest = {'image': 'asaanpay:fixture'}
        config = compose_config(directory, manifest)
        with patch.object(subject, 'docker', return_value=json.dumps(config)) as docker, redirect_stdout(io.StringIO()) as out:
            subject.configuration(directory, manifest)
        self.assertEqual(out.getvalue(), '')
        self.assertEqual(docker.call_args_list[0].args[2], '/dev/null')
        self.assertEqual(docker.call_args_list[1].args[2], '/opt/asaanpay/config/production.env')
        self.assertNotIn('POSTGRES_PASSWORD', docker.call_args_list[1].kwargs['env'])

    def test_live_database_requires_healthy_expected_volume_and_pinned_image(self):
        subject = deploy.Deployer()
        config = compose_config(Path('/release'), {'image': 'asaanpay:fixture'})
        db = {'Id': 'db-container', 'Image': 'db-image', 'State': {'Running': True, 'Health': {'Status': 'healthy'}},
              'Mounts': [{'Destination': '/var/lib/postgresql/data', 'Type': 'volume', 'Name': deploy.VOLUME, 'RW': True}],
              'Config': {'Env': [key + '=' + value for key, value in config['services']['db']['environment'].items()]}}
        volume = {'Name': deploy.VOLUME, 'Driver': 'local', 'Options': None}
        image = {'Id': 'db-image', 'Os': 'linux', 'Architecture': 'amd64'}
        def check(current_db=db, current_volume=volume, current_image=image):
            with patch.object(subject, 'container', return_value=current_db), \
                    patch.object(subject, 'docker', side_effect=[json.dumps([current_volume]), json.dumps([current_image])]):
                return subject.database(config)
        self.assertEqual(check(), 'db-container')
        for bad_db in ({**db, 'State': {'Running': False}}, {**db, 'Mounts': []}, {**db, 'Image': 'other'}):
            with self.assertRaises(deploy.DeploymentError):
                check(current_db=bad_db)
        with self.assertRaises(deploy.DeploymentError):
            check(current_volume={**volume, 'Options': {'device': '/'}})

    def test_slow_successful_calls_cannot_pass_health_deadline(self):
        subject = deploy.Deployer()
        with patch('time.monotonic', side_effect=[0, 0, 121, 121]), \
                patch.object(subject, 'image', return_value='image'), \
                patch.object(subject, 'database', return_value='db'), \
                patch.object(subject, 'network', return_value='network'), \
                patch.object(subject, 'container', return_value={}), self.assertRaises(deploy.DeploymentError):
            subject.health({}, {}, 'db')
        self.assertIsNone(subject.deadline)

    def test_standalone_script_needs_no_repository_imports_and_cli_rejects_bad_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / 'deploy_ec2.py'
            shutil.copyfile(deploy.__file__, script)
            result = subprocess.run([os.sys.executable, '-I', str(script), '../bad'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn('Malformed release ID', result.stderr)


class HardeningTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root / 'package'
        self.manifest = fixture(self.directory)
        self.subject = deploy.Deployer(self.root)

    def test_shared_network_configuration_changes_are_rejected(self):
        for field, value in (('name', 'other'), ('driver', 'overlay'), ('internal', True),
                             ('ipam', {'config': [{'subnet': '10.1.0.0/16'}]}), ('labels', {'custom': 'x'}),
                             ('attachable', True), ('external', True), ('enable_ipv6', True),
                             ('enable_ipv4', False), ('driver_opts', {'unsafe': 'value'}), ('unknown', False)):
            config = compose_config(self.directory, self.manifest)
            config['networks'] = {'default': {'name': deploy.NETWORK, field: value}}
            with self.subTest(field=field), self.assertRaises(deploy.DeploymentError):
                deploy.validate_compose(config, self.directory, self.manifest)
        for field, value in (('network_mode', 'none'), ('networks', {'other': None}),
                             ('networks', {'default': {'aliases': ['db']}})):
            config = compose_config(self.directory, self.manifest)
            config['services']['api'][field] = value
            with self.assertRaises(deploy.DeploymentError):
                deploy.validate_compose(config, self.directory, self.manifest)

    def live_network(self):
        return {'Id': 'network-id', 'Name': deploy.NETWORK, 'Driver': 'bridge', 'Scope': 'local',
                'Internal': False, 'Attachable': False, 'Ingress': False, 'EnableIPv6': False,
                'IPAM': {'Driver': 'default', 'Options': None, 'Config': [{'Subnet': '172.20.0.0/16', 'Gateway': '172.20.0.1'}]},
                'Options': {}, 'Labels': {'com.docker.compose.project': 'asaanpay', 'com.docker.compose.network': 'default'},
                'Containers': {'db': {}}}

    def test_live_network_identity_and_settings_and_db_attachment(self):
        network = self.live_network()
        db = {'Id': 'db', 'NetworkSettings': {'Networks': {deploy.NETWORK: {'NetworkID': 'network-id', 'Aliases': ['db']}}}}
        def check(value, container=db):
            with patch.object(self.subject, 'docker', return_value=json.dumps([value])), patch.object(self.subject, 'container', return_value=container):
                return self.subject.network('db')
        self.assertEqual(check(network), 'network-id')
        for field, value in (('Id', 'replacement'), ('Internal', True), ('Driver', 'overlay'),
                             ('Name', 'other'), ('Options', {'unsafe': 'value'}), ('Containers', {}),
                             ('IPAM', {'Driver': 'default', 'Config': [{'Subnet': '172.21.0.0/16', 'Gateway': '172.21.0.1'}]})):
            with self.subTest(field=field), self.assertRaises(deploy.DeploymentError):
                check({**network, field: value})
        with self.assertRaises(deploy.DeploymentError):
            check(network, {'Id': 'db', 'NetworkSettings': {}})

    def test_projection_contains_no_database_or_managed_resources_and_escapes_secrets(self):
        config = compose_config(self.directory, self.manifest)
        config['services']['api']['depends_on'] = {'db': {'condition': 'service_healthy'}}
        config['services']['api']['environment']['POSTGRES_PASSWORD'] = 'literal${DO_NOT_EXPAND}'
        value = json.loads(self.subject.api_configuration(config, 'verified-network-id'))
        self.assertEqual(set(value['services']), {'api'})
        self.assertNotIn('depends_on', value['services']['api'])
        self.assertNotIn('volumes', value)
        self.assertEqual(value['networks'], {'default': {'external': True, 'name': 'verified-network-id'}})
        self.assertEqual(value['services']['api']['environment']['POSTGRES_PASSWORD'], 'literal$${DO_NOT_EXPAND}')

    def test_metadata_and_package_limits(self):
        with patch.dict(deploy.FILE_LIMITS, {'manifest.json': 10}), self.assertRaisesRegex(deploy.DeploymentError, 'size limit'):
            deploy.check_checksums(self.directory)
        with patch.object(deploy, 'PACKAGE_LIMIT', 10), self.assertRaisesRegex(deploy.DeploymentError, 'size limit'):
            deploy.check_checksums(self.directory)
        with self.assertRaisesRegex(deploy.DeploymentError, 'Metadata size'):
            deploy.bounded_read(self.directory / 'manifest.json', 10)

    def test_download_caps_checked_before_any_get(self):
        self.subject.releases.mkdir()
        calls = []
        def head(*args, **kwargs):
            calls.append(args)
            return json.dumps({'ContentLength': deploy.FILE_LIMITS[args[-1].split('/', 2)[-1]] + 1, 'ETag': '"a"'})
        with patch.object(self.subject, 'run', side_effect=head), self.assertRaisesRegex(deploy.DeploymentError, 'size limit'):
            self.subject.stage(self.manifest['release_id'])
        self.assertTrue(all(c[2] == 'head-object' for c in calls))
        self.assertFalse(list(self.subject.releases.iterdir()))

    def test_aggregate_download_limit_and_low_disk_precede_get(self):
        self.subject.releases.mkdir()
        head = json.dumps({'ContentLength': 10, 'ETag': '"a"'})
        with patch.object(self.subject, 'run', return_value=head) as run, patch.object(deploy, 'PACKAGE_LIMIT', 49):
            with self.assertRaisesRegex(deploy.DeploymentError, 'Aggregate'):
                self.subject.stage(self.manifest['release_id'])
            self.assertEqual(run.call_count, 5)
        with patch.object(self.subject, 'run', return_value=head) as run, patch('shutil.disk_usage', return_value=type('Usage', (), {'free': 0})()):
            with self.assertRaisesRegex(deploy.DeploymentError, 'headroom'):
                self.subject.stage(self.manifest['release_id'])
            self.assertEqual(run.call_count, 5)

    def test_expanded_archive_and_inspection_deadline(self):
        with patch.object(deploy, 'EXPANDED_LIMIT', 10), self.assertRaisesRegex(deploy.DeploymentError, 'Expanded'):
            deploy.validate_archive(self.directory / 'image.tar.gz', self.manifest)
        with patch('time.monotonic', side_effect=[0, deploy.ARCHIVE_SECONDS + 1]), self.assertRaisesRegex(deploy.DeploymentError, 'deadline'):
            deploy.validate_archive(self.directory / 'image.tar.gz', self.manifest)

    def hybrid_archive(self, compressed=True):
        import gzip
        with tarfile.open(self.directory / 'image.tar.gz', 'r:gz') as archive:
            config = archive.extractfile('config.json').read()
        config_id = hashlib.sha256(config).hexdigest()
        layer = gzip.compress(b'', mtime=0) if compressed else b''
        layer_id = hashlib.sha256(layer).hexdigest()
        payloads = {'blobs/sha256/' + config_id: config, 'blobs/sha256/' + layer_id: layer}
        def blob(obj):
            data = json.dumps(obj, sort_keys=True).encode()
            identity = hashlib.sha256(data).hexdigest()
            payloads['blobs/sha256/' + identity] = data
            return {'digest': 'sha256:' + identity, 'size': len(data)}
        descriptor = blob({'schemaVersion': 2, 'config': {'digest': 'sha256:' + config_id, 'size': len(config)},
                           'layers': [{'digest': 'sha256:' + layer_id, 'size': len(layer),
                                       'mediaType': 'application/vnd.oci.image.layer.v1.tar' + ('+gzip' if compressed else '')}]})
        descriptor['mediaType'] = 'application/vnd.oci.image.manifest.v1+json'
        index = blob({'schemaVersion': 2, 'manifests': [descriptor]})
        index['mediaType'] = 'application/vnd.oci.image.index.v1+json'
        payloads['index.json'] = json.dumps({'schemaVersion': 2, 'manifests': [index]}).encode()
        payloads['oci-layout'] = b'{"imageLayoutVersion":"1.0.0"}'
        payloads['manifest.json'] = json.dumps([{'Config': 'blobs/sha256/' + config_id, 'RepoTags': [self.manifest['image']],
                                               'Layers': ['blobs/sha256/' + layer_id]}]).encode()
        self.write_archive(payloads)
        return {**self.manifest, 'image_id': index['digest']}, payloads, descriptor

    def write_archive(self, payloads):
        with tarfile.open(self.directory / 'image.tar.gz', 'w:gz') as archive:
            for name, data in payloads.items():
                entry = tarfile.TarInfo(name)
                entry.size = len(data)
                archive.addfile(entry, io.BytesIO(data))

    def test_classic_and_hybrid_config_manifest_index_identity(self):
        classic = deploy.validate_archive(self.directory / 'image.tar.gz', self.manifest)
        self.assertEqual(classic['config_id'], self.manifest['image_id'])
        manifest, _, descriptor = self.hybrid_archive()
        for identity in (manifest['image_id'], self.manifest['image_id'], descriptor['digest']):
            result = deploy.validate_archive(self.directory / 'image.tar.gz', {**manifest, 'image_id': identity})
            self.assertEqual(result['config_id'], self.manifest['image_id'])
        self.subject.identities[manifest['release_id']] = result
        with tarfile.open(self.directory / 'image.tar.gz', 'r:gz') as archive:
            config = json.load(archive.extractfile('blobs/sha256/' + result['config_id'][7:]))
        info = {'Id': result['config_id'], 'Os': 'linux', 'Architecture': 'amd64', 'Config': config['config']}
        with patch.object(self.subject, 'docker', return_value=json.dumps([info])):
            self.assertEqual(self.subject.image(manifest), result['config_id'])
        with patch.object(self.subject, 'docker', return_value=json.dumps([{**info, 'Id': 'sha256:' + '0' * 64}])), self.assertRaises(deploy.DeploymentError):
            self.subject.image(manifest)

    def test_hybrid_alternate_tag_is_rejected_before_loading(self):
        manifest, payloads, _ = self.hybrid_archive()
        root = json.loads(payloads['index.json'])
        root['manifests'][0]['annotations'] = {'io.containerd.image.name': 'postgres:unsafe'}
        payloads['index.json'] = json.dumps(root).encode()
        self.write_archive(payloads)
        with self.assertRaisesRegex(deploy.DeploymentError, 'OCI archive tag mismatch'):
            deploy.validate_archive(self.directory / 'image.tar.gz', manifest)

    def test_hybrid_tampered_blob_and_unsupported_export_fail_closed(self):
        manifest, payloads, descriptor = self.hybrid_archive()
        payloads['blobs/sha256/' + descriptor['digest'][7:]] += b' '
        self.write_archive(payloads)
        with self.assertRaisesRegex(deploy.DeploymentError, 'OCI blob identity'):
            deploy.validate_archive(self.directory / 'image.tar.gz', manifest)
        del payloads['manifest.json']
        self.write_archive(payloads)
        with self.assertRaisesRegex(deploy.DeploymentError, 'unsupported Docker-save'):
            deploy.validate_archive(self.directory / 'image.tar.gz', manifest)

    def api(self):
        return {'Id': 'exact-container-id', 'Image': self.manifest['image_id'],
                'State': {'Running': True, 'Health': {'Status': 'healthy'}},
                'Config': {'Labels': {'io.asaanpay.release-id': self.manifest['release_id']}},
                'HostConfig': {'PortBindings': {'8000/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '8000'}]}},
                'NetworkSettings': {'Ports': {'8000/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '8000'}]},
                                    'Networks': {deploy.NETWORK: {'NetworkID': 'network-id'}}}}

    def response(self):
        return {'health': {'status': 'ok', 'database': 'connected'},
                'version': {k: self.manifest[k] for k in ('version', 'commit')},
                'metadata': deploy.validate_manifest(self.manifest)}

    def test_exact_container_responder_and_full_release_identity(self):
        self.subject.deadline = time.monotonic() + 10
        with patch.object(self.subject, 'docker', return_value=json.dumps(self.response())) as docker:
            self.subject.responder(self.api(), self.manifest)
            self.assertEqual(docker.call_args.args[:3], ('exec', 'exact-container-id', 'python'))
            self.assertIn('ProxyHandler({})', docker.call_args.args[4])
        wrong = self.response()
        wrong['metadata']['run_attempt'] = '2'
        with patch.object(self.subject, 'docker', return_value=json.dumps(wrong)), self.assertRaises(deploy.DeploymentError):
            self.subject.responder(self.api(), self.manifest)

    def test_old_listener_cannot_substitute_for_port_mismatch(self):
        self.subject.deadline = time.monotonic() + 10
        for field in ('HostConfig', 'NetworkSettings'):
            api = self.api()
            api[field] = {}
            with patch.object(self.subject, 'docker') as docker, self.assertRaisesRegex(deploy.DeploymentError, 'port bindings'):
                self.subject.responder(api, self.manifest)
            docker.assert_not_called()

    def test_health_success_after_deadline_is_rejected(self):
        clock = [0.0]
        def slow_success(api, manifest):
            clock[0] = 121.0
        with patch('time.monotonic', side_effect=lambda: clock[0]), \
                patch.object(self.subject, 'image', return_value=self.manifest['image_id']), \
                patch.object(self.subject, 'database', return_value='db'), \
                patch.object(self.subject, 'network', return_value='network-id'), \
                patch.object(self.subject, 'container', return_value=self.api()), \
                patch.object(self.subject, 'responder', side_effect=slow_success), \
                self.assertRaisesRegex(deploy.DeploymentError, 'deadline'):
            self.subject.health(self.manifest, {}, 'db')

    def test_verified_health_success_and_remaining_subprocess_budget(self):
        with patch.object(self.subject, 'image', return_value=self.manifest['image_id']), \
                patch.object(self.subject, 'database', return_value='db'), \
                patch.object(self.subject, 'network', return_value='network-id'), \
                patch.object(self.subject, 'container', return_value=self.api()), \
                patch.object(self.subject, 'docker', return_value=json.dumps(self.response())) as docker:
            self.subject.health(self.manifest, {}, 'db')
        self.assertLessEqual(docker.call_args.kwargs['timeout'], 10)
        self.subject.deadline = time.monotonic() - 1
        with patch('subprocess.Popen') as popen, self.assertRaisesRegex(deploy.DeploymentError, 'deadline'):
            self.subject.run('never-start')
        popen.assert_not_called()

    def test_archive_worker_is_standalone_on_valid_package(self):
        script = self.root / 'standalone.py'
        shutil.copyfile(deploy.__file__, script)
        result = subprocess.run([sys.executable, '-I', '-B', str(script), '--inspect-archive', str(self.directory)],
                                cwd=self.root, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['config_id'], self.manifest['image_id'])
        identity = self.subject.inspect_archive(self.directory, self.manifest)
        self.assertEqual(identity['publisher_id'], self.manifest['image_id'])

    def test_archive_member_count_and_nested_decompression_limits(self):
        self.write_archive({str(n): b'' for n in range(4097)})
        with self.assertRaisesRegex(deploy.DeploymentError, 'excessive archive members'):
            deploy.validate_archive(self.directory / 'image.tar.gz', self.manifest)
        import gzip
        self.write_archive({'layer.tar': gzip.compress(b'0' * 100000, mtime=0)})
        with patch.object(deploy, 'EXPANDED_LIMIT', 10000), self.assertRaisesRegex(deploy.DeploymentError, 'Expanded'):
            deploy.validate_archive(self.directory / 'image.tar.gz', self.manifest)

    def test_preflight_pins_reviewed_engine_and_compose_versions(self):
        with patch('os.geteuid', return_value=0), patch('platform.system', return_value='Linux'), \
                patch('platform.machine', return_value='x86_64'), patch.object(deploy, 'secure_path'):
            for versions in (['2.40.3', '25.0.6'], ['2.40.3', '25.0.18'], ['5.6.0'], ['2.40.3', '29.0.0']):
                with patch.object(self.subject, 'docker', side_effect=versions):
                    if len(versions) == 2 and versions[1].startswith('25.'):
                        self.subject.preflight()
                    else:
                        with self.assertRaisesRegex(deploy.DeploymentError, 'version not validated'):
                            self.subject.preflight()


class ProcessSupervisionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.subject = deploy.Deployer(self.root)

    def tree(self, ready_prefix=None):
        # A harmless grandchild ignores SIGTERM, requiring group SIGKILL.
        ready = ("Path(" + repr(str(ready_prefix)) + " + str(os.getpid())).write_text('ready'); "
                 if ready_prefix is not None else '')
        return ('import os,signal,time; from pathlib import Path; '
                'Path(' + repr(str(self.root / 'pgid')) + ').write_text(str(os.getpgrp())); '
                'signal.signal(signal.SIGTERM, signal.SIG_IGN); os.fork(); ' + ready + 'time.sleep(60)')

    def test_timeout_kills_and_reaps_group_before_unlock(self):
        with deploy.deployment_lock(self.root) as fd:
            self.subject.lock_fd = fd
            with self.assertRaises(deploy.DeploymentError):
                self.subject.run(sys.executable, '-c', self.tree(), timeout=0.4)
            pgid = int((self.root / 'pgid').read_text())
            self.assertFalse(deploy.group_alive(pgid))
            with self.assertRaises(deploy.DeploymentError):
                with deploy.deployment_lock(self.root):
                    pass
        with deploy.deployment_lock(self.root):
            pass

    def test_sigterm_and_sigint_cancel_and_reap_descendants(self):
        import threading
        for sig in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(sig=sig), deploy.cancellation_handlers():
                # Emulated Linux startup can exceed 400 ms. Signal only after
                # BOTH processes exist, so this actually tests descendant cleanup.
                prefix = 'ready-' + str(int(sig)) + '-'
                ready, stop = threading.Event(), threading.Event()
                def cancel_when_ready():
                    deadline = time.monotonic() + 10
                    while not stop.wait(0.01) and time.monotonic() < deadline:
                        if len(list(self.root.glob(prefix + '*'))) == 2:
                            ready.set()
                            os.kill(os.getpid(), sig)
                            return
                sender = threading.Thread(target=cancel_when_ready)
                sender.start()
                try:
                    with self.assertRaises(deploy.Cancelled):
                        self.subject.run(sys.executable, '-c', self.tree(self.root / prefix), timeout=15)
                finally:
                    stop.set()
                    sender.join()
                self.assertTrue(ready.is_set(), 'Child and grandchild never became ready')
            self.assertFalse(deploy.group_alive(int((self.root / 'pgid').read_text())))

    def test_orphan_on_success_is_not_a_success(self):
        script = ('import os,time; from pathlib import Path; '
                  'Path(' + repr(str(self.root / 'pgid')) + ').write_text(str(os.getpgrp())); '
                  'pid=os.fork(); '
                  'os._exit(0) if pid else None; '
                  'os.close(1); os.close(2); time.sleep(60)')
        with self.assertRaisesRegex(deploy.DeploymentError, 'descendants'):
            self.subject.run(sys.executable, '-c', script, timeout=2)
        self.assertFalse(deploy.group_alive(int((self.root / 'pgid').read_text())))

    def test_output_limit_and_input_are_supervised(self):
        with patch.object(deploy, 'OUTPUT_LIMIT', 100), self.assertRaisesRegex(deploy.DeploymentError, 'output limit'):
            self.subject.run(sys.executable, '-c', 'print("x"*1000)')
        self.assertEqual(self.subject.run(sys.executable, '-c', 'import sys; print(len(sys.stdin.read()))', input_text='x' * 100000), '100000')

    def test_uncertain_termination_retains_lock_until_group_gone(self):
        from unittest.mock import Mock
        process = Mock(pid=123, returncode=0)
        with deploy.deployment_lock(self.root):
            def still_locked(_):
                with self.assertRaises(deploy.DeploymentError):
                    with deploy.deployment_lock(self.root):
                        pass
            with patch.object(deploy, 'reap_group'), patch.object(deploy.os, 'killpg'), \
                    patch.object(deploy, 'group_alive', side_effect=[True, True, True, False]), \
                    patch('time.monotonic', side_effect=[0, 3, 3, 6]), \
                    patch('time.sleep', side_effect=still_locked), redirect_stderr(io.StringIO()) as output:
                deploy.stop_group(process)
            self.assertIn('retaining lock', output.getvalue())
        process.wait.assert_called_once()

    def test_cancellation_during_process_launch_still_reaps_child(self):
        original = subprocess.Popen
        observed = []
        def launch(*args, **kwargs):
            process = original(*args, **kwargs)
            observed.append(process)
            os.kill(os.getpid(), signal.SIGTERM)
            return process
        with deploy.cancellation_handlers(), patch('subprocess.Popen', side_effect=launch), self.assertRaises(deploy.Cancelled):
            self.subject.run(sys.executable, '-c', 'import time; time.sleep(60)')
        self.assertEqual(len(observed), 1)
        self.assertFalse(deploy.group_alive(observed[0].pid))

    def test_sigkill_leaves_durable_journal_and_inherited_lock(self):
        script = '''import sys
from pathlib import Path
from scripts import deploy_ec2 as d
subject=d.Deployer(Path(sys.argv[1]))
state={'current':None,'in_progress':{'release_id':'interrupted'}}
d.atomic_state(subject.state_file,state)
with d.deployment_lock(subject.base) as fd:
    subject.lock_fd=fd
    subject.active_state=state
    subject.run(sys.executable,'-c','import time; time.sleep(60)')
'''
        # Parent subreaper adopts the worker's orphan on Linux too.
        if sys.platform == 'linux':
            self.assertEqual(deploy.ctypes.CDLL(None).prctl(36, 1, 0, 0, 0), 0)
        worker = subprocess.Popen([sys.executable, '-B', '-c', script, str(self.root)],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        pgid = None
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if self.subject.state_file.exists():
                    state = json.loads(self.subject.state_file.read_text())
                    pgid = state['in_progress'].get('operation_pgid')
                    if pgid:
                        break
                time.sleep(0.02)
            self.assertIsNotNone(pgid)
            worker.kill()
            worker.wait(timeout=5)
            self.assertTrue(deploy.group_alive(pgid))
            self.assertIsNotNone(json.loads(self.subject.state_file.read_text())['in_progress'])
            with self.assertRaises(deploy.DeploymentError):
                with deploy.deployment_lock(self.root):
                    pass
        finally:
            if worker.poll() is None:
                worker.kill()
            worker.wait(timeout=5)
            if pgid:
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                if sys.platform == 'linux':
                    os.waitpid(pgid, 0)
                end = time.monotonic() + 5
                while deploy.group_alive(pgid) and time.monotonic() < end:
                    time.sleep(0.02)
        self.assertFalse(deploy.group_alive(pgid))
        with deploy.deployment_lock(self.root):
            pass
