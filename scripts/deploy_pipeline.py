"""Same-run automatic deployment orchestration; AWS calls occur only after commissioning.

CI compatibility is supplied by a separately reviewed, checksum-pinned checker.
This module neither invents that checker nor treats matching SQL as its evidence.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time
from urllib.request import Request, build_opener, HTTPRedirectHandler
from uuid import UUID, uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts import deploy_ec2 as deploy
from scripts import publish_release as publisher
from scripts import ssm_deploy as bootstrap
from scripts.ssm_deploy import contract_facts

ROOT = Path(__file__).resolve().parent.parent
REPOSITORY = 'Batsy1857/AsaanPay'
BRANCH = 'feature/aws-cicd-devsecops'
INSTANCE = 'i-00fc050cde50b051e'
POLICY = ROOT / '.github/deployment-policy.json'
CHECKER = ROOT / 'scripts/demo_compatibility.py'
REQUIRED_JOBS = {'Python, PostgreSQL and browser tests', 'Bandit SAST', 'Gitleaks history scan',
                 'Python dependency audit', 'Versioned image, Trivy and release artifact',
                 'Publish verified release to private S3'}
require = deploy.require


def sha(data):
    return hashlib.sha256(data).hexdigest()


def inputs(environment):
    expected = publisher.expected_identity(environment)
    require(environment.get('PUBLISHED_RELEASE_ID') == expected['release_id'], 'Published release ID differs from this CI run')
    require(environment.get('PUBLICATION_RESULT') == 'success', 'Publication did not succeed')
    require(re.fullmatch(r'[0-9a-f]{64}', environment.get('PUBLICATION_SHA256', '')), 'Missing publication marker digest')
    require(re.fullmatch(r'[1-9][0-9]*', environment.get('GITHUB_RUN_NUMBER', '')), 'Missing workflow sequence number')
    return expected


def record_publication(directory, environment):
    expected = publisher.expected_identity(environment)
    require(environment.get('BUILT_RELEASE_ID') == expected['release_id'], 'Build output identity mismatch')
    publisher.validate_package(directory, expected)
    return {'release-id': expected['release_id'], 'marker-sha256': deploy.digest(directory / 'SHA256SUMS')}


def read_policy(path=None, commissioned=False):
    path = POLICY if path is None else path
    raw = deploy.bounded_read(path, 65536)
    policy = json.loads(raw)
    require(policy.get('schema_version') == 1 and type(policy.get('commissioned')) is bool, 'Invalid commissioning policy')
    if not commissioned:
        return policy, sha(raw)
    require(policy['commissioned'] is True, 'Database compatibility policy is not commissioned')
    approval, contract = policy.get('approval'), policy.get('contract')
    require(isinstance(approval, dict) and all(isinstance(approval.get(k), str) and approval[k].strip()
            for k in ('reviewer', 'review_reference')), 'Missing reviewed policy approval')
    require(isinstance(contract, dict) and contract.get('no_database_changes') is True, 'Missing application-only database contract')
    require(re.fullmatch(r'[a-zA-Z0-9._-]{1,80}', contract.get('id', '')), 'Invalid contract ID')
    deploy.release_identity(contract.get('baseline_release_id', ''))
    for key in ('schema_sha256', 'database_service_sha256', 'networks_sha256', 'volumes_sha256', 'checker_sha256'):
        require(re.fullmatch(r'[0-9a-f]{64}', contract.get(key, '')), 'Missing approved contract checksum: ' + key)
    require(re.fullmatch(r'postgres:17-alpine@sha256:[0-9a-f]{64}', contract.get('postgres_image', '')), 'Missing approved database image')
    checks = contract.get('required_checks')
    require(isinstance(checks, list) and checks and len(set(checks)) == len(checks)
            and all(isinstance(c, str) and re.fullmatch(r'[a-zA-Z0-9._-]{1,80}', c) for c in checks), 'Missing compatibility test requirements')
    bootstrap.validate_demo_contract(contract)
    return policy, sha(raw)


def configuration(directory, manifest):
    env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'POSTGRES_USER': 'contract',
           'POSTGRES_PASSWORD': 'dummy-only', 'POSTGRES_DB': 'contract', 'ASAANPAY_IMAGE': manifest['image']}
    result = subprocess.run(['docker', 'compose', '--env-file', '/dev/null', '--project-directory', str(directory),
                             '-f', str(directory / 'compose.prod.yaml'), 'config', '--format', 'json'],
                            env=env, check=True, capture_output=True, text=True, timeout=30)
    value = json.loads(result.stdout)
    deploy.validate_compose(value, directory, manifest)
    return value


def check_contract(facts, policy):
    require(all(policy['contract'].get(k) == v for k, v in facts.items()), 'Candidate changes the approved database/network/volume contract')


def check_evidence(value, expected, manifest, policy, policy_sha):
    require(value.get('schema_version') == 1 and value.get('passed') is True
            and value.get('release_id') == expected['release_id'] and value.get('image_id') == manifest['image_id']
            and value.get('policy_sha256') == policy_sha and value.get('contract_id') == policy['contract']['id'],
            'Compatibility evidence identity or result mismatch')
    require(value.get('live_schema_sha256') == policy['contract']['live_schema_sha256'], 'Compatibility evidence schema mismatch')
    require(value.get('checks') == {key: True for key in policy['contract']['required_checks']}
            and all(v is True for v in value['checks'].values()), 'Compatibility checks missing or failed')


def authorize(directory, environment):
    expected = inputs(environment)
    manifest = publisher.validate_package(directory, expected)
    require(deploy.digest(directory / 'SHA256SUMS') == environment['PUBLICATION_SHA256'], 'Artifact differs from publication receipt')
    require(environment.get('PRODUCTION_DEPLOYMENT_ENABLED') == 'true', 'Production execution is disabled')
    policy, policy_sha = read_policy(commissioned=True)
    facts = contract_facts(directory, configuration(directory, manifest))
    check_contract(facts, policy)
    require(CHECKER.is_file() and not CHECKER.is_symlink() and deploy.digest(CHECKER) == policy['contract']['checker_sha256'],
            'Reviewed compatibility checker is missing or changed')
    for name, checksum in policy['contract']['dependency_sha256'].items():
        path = ROOT / name
        require(not path.is_symlink() and not path.parent.is_symlink() and path.is_file()
                and deploy.digest(path) == checksum, 'Reviewed checker dependency changed: ' + name)
    require(not (ROOT / 'scripts/__init__.py').exists(), 'Unexpected checker package initializer')
    # No OIDC credentials have been obtained at this step. The checker must use
    # disposable CI database resources and the approved baseline, never production.
    with tempfile.TemporaryDirectory(prefix='asaanpay-compatibility-') as temporary:
        output = Path(temporary) / 'evidence.json'
        subprocess.run([sys.executable, str(CHECKER), '--package', str(directory), '--policy', str(POLICY), '--output', str(output)],
                       check=True, timeout=480, capture_output=True, text=True,
                       env={key: os.environ[key] for key in ('PATH', 'HOME') if key in os.environ})
        value = json.loads(deploy.bounded_read(output, 65536))
        check_evidence(value, expected, manifest, policy, policy_sha)
    return {'expected': expected, 'image_id': manifest['image_id'], 'policy_sha256': policy_sha,
            'facts': facts, 'evidence': value, 'marker_sha256': environment['PUBLICATION_SHA256'],
            'script_sha256': deploy.digest(ROOT / 'scripts/deploy_ec2.py')}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def github(path):
    request = Request('https://api.github.com/repos/' + REPOSITORY + path, headers={
        'Authorization': 'Bearer ' + os.environ['GH_TOKEN'], 'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28'})
    with build_opener(NoRedirect()).open(request, timeout=20) as response:
        raw = response.read(2 * 1024 * 1024 + 1)
    require(len(raw) <= 2 * 1024 * 1024, 'Excessive GitHub response')
    return json.loads(raw)


def provenance(expected, environment):
    run_id, attempt = expected['run_id'], expected['run_attempt']
    run = github(f'/actions/runs/{run_id}/attempts/{attempt}')
    require(str(run.get('id')) == run_id and str(run.get('run_attempt')) == attempt
            and str(run.get('run_number')) == environment['GITHUB_RUN_NUMBER']
            and run.get('head_sha') == expected['commit'] and run.get('head_branch') == BRANCH
            and run.get('event') == 'push' and run.get('path', '').split('@')[0] == '.github/workflows/ci.yml'
            and run.get('repository', {}).get('full_name') == REPOSITORY
            and run.get('head_repository', {}).get('full_name') == REPOSITORY, 'CI provenance mismatch')
    # This CI run is still in progress because deployment is one of its jobs.
    # Verify its prerequisite jobs, not an impossible overall-success conclusion.
    jobs = []
    for page in range(1, 11):
        part = github(f'/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100&page={page}')['jobs']
        jobs.extend(part)
        if len(part) < 100:
            break
    else:
        raise ValueError('Excessive CI job pages')
    for name in REQUIRED_JOBS:
        found = [job for job in jobs if job.get('name') == name]
        require(len(found) == 1 and found[0].get('status') == 'completed' and found[0].get('conclusion') == 'success',
                'Required CI job did not succeed: ' + name)
    require(type(run.get('workflow_id')) is int and run['workflow_id'] > 0, 'Missing CI workflow identity')
    return run['workflow_id']


class AWSFailure(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__('AWS request failed: ' + code)


def aws(service, operation, *arguments, timeout=60):
    result = subprocess.run(['aws', service, operation, *arguments, '--region', 'us-east-1', '--output', 'json',
                             '--cli-connect-timeout', '5', '--cli-read-timeout', '20'],
                            capture_output=True, text=True, timeout=timeout,
                            env={**os.environ, 'AWS_MAX_ATTEMPTS': '1', 'AWS_PAGER': '', 'AWS_CLI_AUTO_PROMPT': 'off'})
    if result.returncode:
        code = next((c for c in ('InvocationDoesNotExist', 'PreconditionFailed', 'ConditionalRequestConflict',
                                'AccessDenied', 'AccessDeniedException', 'NoSuchKey', 'InvalidInstanceId',
                                'ExpiredToken', 'ThrottlingException') if '(' + c + ')' in result.stderr), 'Unclassified')
        raise AWSFailure(code)
    return json.loads(result.stdout)


def get_object(key, destination, limit, expected_size=None):
    common = ('--bucket', 'asaanpay-releases', '--key', key, '--expected-bucket-owner', publisher.ACCOUNT)
    info = aws('s3api', 'head-object', *common)
    size, etag = info.get('ContentLength'), info.get('ETag', '')
    require(type(size) is int and 0 < size <= limit and (expected_size is None or size == expected_size), 'S3 object size mismatch')
    require(re.fullmatch(r'"[0-9a-fA-F]+(?:-[0-9]+)?"', etag), 'Invalid S3 ETag')
    aws('s3api', 'get-object', *common, '--if-match', etag, '--range', f'bytes=0-{size - 1}', str(destination))
    require(destination.stat().st_size == size, 'Incomplete S3 object')


def verify_publication(directory, expected, marker):
    manifest = publisher.validate_package(directory, expected)
    require(deploy.digest(directory / 'SHA256SUMS') == marker, 'Publication marker differs from same-run artifact')
    with tempfile.TemporaryDirectory(prefix='asaanpay-publication-') as temporary:
        for name in ('SHA256SUMS', 'manifest.json'):
            destination = Path(temporary) / name
            get_object('releases/' + expected['release_id'] + '/' + name, destination, deploy.FILE_LIMITS[name], (directory / name).stat().st_size)
            require(deploy.digest(destination) == deploy.digest(directory / name), 'S3 publication differs from approved artifact')
        # The host will download and hash all five files against this pinned marker.
        for name in deploy.PACKAGE_FILES:
            info = aws('s3api', 'head-object', '--bucket', 'asaanpay-releases', '--expected-bucket-owner', publisher.ACCOUNT,
                       '--key', 'releases/' + expected['release_id'] + '/' + name)
            require(info.get('ContentLength') == (directory / name).stat().st_size, 'Published package file missing or changed')
    return manifest


def deliver_script(expected_sha):
    path = ROOT / 'scripts/deploy_ec2.py'
    require(path.is_file() and not path.is_symlink() and 0 < path.stat().st_size <= 262144, 'Unsafe or oversized deployer source')
    require(deploy.digest(path) == expected_sha, 'Deployer changed after authorization')
    key = 'releases/deployment-tools/' + expected_sha + '/deploy_ec2.py'
    try:
        aws('s3api', 'put-object', '--bucket', 'asaanpay-releases', '--expected-bucket-owner', publisher.ACCOUNT,
            '--key', key, '--body', str(path), '--if-none-match', '*')
    except AWSFailure as error:
        if error.code != 'PreconditionFailed':
            raise
        # A content-addressed tool may already exist. Verify bytes; never overwrite.
    with tempfile.TemporaryDirectory(prefix='asaanpay-tool-') as temporary:
        target = Path(temporary) / 'deployer.py'
        get_object(key, target, 262144, path.stat().st_size)
        require(deploy.digest(target) == expected_sha, 'Immutable deployer object checksum mismatch')
    return key


def remote_command(request):
    bootstrap.validate_request(request)
    source = (ROOT / 'scripts/ssm_deploy.py').read_bytes()
    payload = base64.b64encode(json.dumps(request, sort_keys=True).encode()).decode()
    code = 'import base64;exec(compile(base64.b64decode(' + repr(base64.b64encode(source).decode()) + "),'<reviewed-bootstrap>','exec'))"
    args = ['systemd-run', '--unit=asaanpay-ci-deploy', '--wait', '--collect', '--service-type=exec',
            '--property=RuntimeMaxSec=600', '--property=TimeoutStopSec=30', '--property=KillMode=control-group',
            '--property=SendSIGKILL=yes', '--property=UMask=0077', '--property=StandardOutput=journal',
            '--property=StandardError=journal', '/usr/bin/python3', '-c', code, payload]
    # A receipt is required even if systemd treats a signal as a clean exit.
    check = ("import json,os,stat,sys;from pathlib import Path;"
             "p=Path('/run/asaanpay-deploy/" + request['nonce'] + ".json');s=p.lstat();"
             "ok=stat.S_ISREG(s.st_mode) and s.st_uid==0 and stat.S_IMODE(s.st_mode)==0o600 and s.st_size<=4096;"
             "sys.exit(1) if not ok else None;"
             "ok=json.loads(p.read_text())==" + repr({'nonce': request['nonce'], 'release_id': request['expected']['release_id'],
                  'marker_sha256': request['marker_sha256']}) + ";sys.exit(1) if not ok else None;print('ASAANPAY_DEPLOY_OK " + request['nonce'] + "')")
    command = 'set -eu\n' + shlex.join(args) + '\n' + shlex.join(['/usr/bin/python3', '-c', check])
    require(len(command.encode()) < 48000, 'SSM command exceeds reviewed size budget')
    return command


def send_and_wait(request, report, clock=time.monotonic, sleep=time.sleep):
    deadline = clock() + 780
    command = remote_command(request)
    # A transport timeout can occur after AWS accepts the command. Never label
    # that outcome "not sent" or retry a potentially accepted submission.
    report.update(command_id='Not returned (submission attempted)', status='SSM submission outcome unknown')
    result = aws('ssm', 'send-command', '--instance-ids', INSTANCE, '--document-name', 'AWS-RunShellScript',
                 '--timeout-seconds', '60', '--comment', 'AsaanPay commissioned application-only deployment',
                 '--parameters', json.dumps({'commands': [command], 'executionTimeout': ['720']}), timeout=30)
    command_id = result['Command']['CommandId']
    require(str(UUID(command_id)) == command_id, 'Invalid SSM command ID')
    report.update(command_id=command_id, status='Pending')
    print('SSM Command ID: ' + command_id, flush=True)
    while clock() < deadline:
        try:
            value = aws('ssm', 'get-command-invocation', '--command-id', command_id, '--instance-id', INSTANCE,
                        timeout=max(0.001, min(30, deadline - clock())))
        except AWSFailure as error:
            if error.code != 'InvocationDoesNotExist':
                raise
        else:
            require(value.get('CommandId') == command_id and value.get('InstanceId') == INSTANCE
                    and value.get('DocumentName') == 'AWS-RunShellScript', 'Unexpected SSM invocation identity')
            status = value.get('Status')
            report['status'] = status if status in ('Pending', 'InProgress', 'Delayed', 'Success', 'Failed', 'Cancelled', 'Cancelling', 'TimedOut') else 'Unexpected'
            if status not in ('Pending', 'InProgress', 'Delayed'):
                response_code = value.get('ResponseCode')
                diagnostic = ('SSM status ' + report['status'] + ', response code '
                              + (str(response_code) if type(response_code) is int else 'invalid'))
                require(status == 'Success' and value.get('ResponseCode') == 0
                        and ('ASAANPAY_DEPLOY_OK ' + request['nonce']) in value.get('StandardOutputContent', '').splitlines()
                        and clock() < deadline, diagnostic + ': deployment/health verification did not succeed')
                report['health'] = 'PASS'
                return
        sleep(min(5, max(0, deadline - clock())))
    report['status'] = 'Polling timeout; inspect the bounded systemd unit before retrying'
    raise TimeoutError('SSM deadline exceeded; no automatic retry or rollback')


def execute(directory, authorization, environment, report):
    require(environment.get('PRODUCTION_DEPLOYMENT_ENABLED') == 'true', 'Production execution disabled')
    expected = inputs(environment)
    require(authorization['expected'] == expected, 'Authorization is for another run')
    policy, policy_sha = read_policy(commissioned=True)
    require(policy_sha == authorization['policy_sha256'], 'Commissioned policy changed')
    manifest = publisher.validate_package(directory, expected)
    check_evidence(authorization['evidence'], expected, manifest, policy, policy_sha)
    workflow_id = provenance(expected, environment)
    session = 'AsaanPay-Deploy-' + expected['run_id'] + '-' + expected['run_attempt']
    identity = aws('sts', 'get-caller-identity')
    require(identity.get('Account') == publisher.ACCOUNT and identity.get('Arn') ==
            f'arn:aws:sts::{publisher.ACCOUNT}:assumed-role/AsaanPay-GitHubActions-Role/{session}', 'AWS caller identity mismatch')
    target = aws('ssm', 'describe-instance-information', '--filters', json.dumps([{'Key': 'InstanceIds', 'Values': [INSTANCE]}]))
    entries = target.get('InstanceInformationList', [])
    require(len(entries) == 1 and entries[0].get('InstanceId') == INSTANCE and entries[0].get('PingStatus') == 'Online'
            and entries[0].get('PlatformType') == 'Linux' and entries[0].get('ResourceType') == 'EC2Instance', 'Unexpected or unavailable SSM target')
    verify_publication(directory, expected, authorization['marker_sha256'])
    deliver_script(authorization['script_sha256'])
    request = {**authorization, 'workflow_id': workflow_id, 'run_number': int(environment['GITHUB_RUN_NUMBER']),
               'repository': REPOSITORY, 'branch': BRANCH, 'nonce': uuid4().hex}
    send_and_wait(request, report)


def outputs(values):
    with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
        for key, value in values.items():
            stream.write(key + '=' + value + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('publication-proof', 'prepare', 'authorize', 'execute'))
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    report = {'command_id': 'Not sent', 'status': 'Not executed', 'health': 'NOT VERIFIED'}
    expected = None
    try:
        if args.command == 'publication-proof':
            outputs(record_publication(args.directory, os.environ))
            return 0
        expected = inputs(os.environ)
        if args.command == 'prepare':
            publisher.validate_package(args.directory, expected)
            require(deploy.digest(args.directory / 'SHA256SUMS') == os.environ['PUBLICATION_SHA256'], 'Artifact/publication mismatch')
            read_policy()
            report['status'] = 'Production gate disabled' if os.environ.get('PRODUCTION_DEPLOYMENT_ENABLED') != 'true' else 'Prepared; not deployed'
        elif args.command == 'authorize':
            value = authorize(args.directory, os.environ)
            (Path(os.environ['RUNNER_TEMP']) / 'asaanpay-deployment-authorization.json').write_text(json.dumps(value))
            report['status'] = 'Compatibility verified; not deployed'
        else:
            authorization = json.loads(deploy.bounded_read(Path(os.environ['RUNNER_TEMP']) / 'asaanpay-deployment-authorization.json', 262144))
            execute(args.directory, authorization, os.environ, report)
        return 0
    except Exception as error:
        report['status'] = 'FAILED: ' + (str(error) if isinstance(error, (deploy.DeploymentError, AWSFailure, TimeoutError)) else 'details withheld')
        print(report['status'], file=sys.stderr)
        return 1
    finally:
        if args.command != 'publication-proof' and os.environ.get('GITHUB_STEP_SUMMARY'):
            with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as stream:
                stream.write('## Production deployment\n\n' + '\n\n'.join([
                    'Release: ' + (expected or {}).get('release_id', 'Invalid'), 'Commit: ' + (expected or {}).get('commit', 'Invalid'),
                    'SSM command ID: ' + report['command_id'], 'Status: ' + report['status'], 'Health verification: ' + report['health']]) + '\n')


if __name__ == '__main__':
    raise SystemExit(main())
