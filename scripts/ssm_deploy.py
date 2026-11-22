"""Python 3.9 standalone SSM bootstrap, embedded from the exact CI commit.

Only executed by a commissioned workflow in a bounded systemd service. Host
policy/baseline provisioning is deliberately separate; no approval is fabricated.
"""
import base64
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
from urllib.request import Request, ProxyHandler, build_opener

BASE = Path('/opt/asaanpay')
ACCOUNT = '642195693459'
INSTANCE = 'i-00fc050cde50b051e'
ENV = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'AWS_CONFIG_FILE': '/dev/null',
       'AWS_SHARED_CREDENTIALS_FILE': '/dev/null', 'AWS_EC2_METADATA_DISABLED': 'false',
       'AWS_REGION': 'us-east-1', 'AWS_MAX_ATTEMPTS': '1', 'AWS_PAGER': '', 'AWS_CLI_AUTO_PROMPT': 'off'}

CHECKER_DEPENDENCIES = ('app/__init__.py', 'app/version.py', 'scripts/release.py', 'scripts/ssm_deploy.py')
DEMO_CHECKS = ('package_contract', 'image_identity', 'startup_health', 'existing_record',
               'checkout_replay_persistence', 'startup_recovery', 'schema_unchanged')
# One application table, its indexes/constraints and directly required UUID
# function. No data rows, migrations, general-purpose catalog crawler or writes.
SCHEMA_SQL = r"""
BEGIN READ ONLY;
SET LOCAL statement_timeout = '5s';
SET LOCAL lock_timeout = '2s';
SET LOCAL search_path = pg_catalog;
SELECT json_build_object(
 'format', 1, 'server_major', current_setting('server_version_num')::int / 10000,
 'table', (SELECT json_build_object('schema','public','name',c.relname,'kind',c.relkind,
             'persistence',c.relpersistence,'row_security',c.relrowsecurity,
             'force_row_security',c.relforcerowsecurity,'replica_identity',c.relreplident)
           FROM pg_class c WHERE c.oid='public.payment_attempts'::regclass),
 'columns', (SELECT json_agg(v ORDER BY v.position) FROM (
   SELECT a.attnum AS position, a.attname AS name, format_type(a.atttypid,a.atttypmod) AS type,
          a.attnotnull AS not_null, pg_get_expr(d.adbin,d.adrelid) AS default_expression,
          a.attidentity AS identity, a.attgenerated AS generated,
          CASE WHEN a.attcollation=0 THEN NULL ELSE a.attcollation::regcollation::text END AS collation
   FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
   WHERE a.attrelid='public.payment_attempts'::regclass AND a.attnum>0 AND NOT a.attisdropped) v),
 'constraints', (SELECT COALESCE(json_agg(v ORDER BY v.name),'[]'::json) FROM (
   SELECT conname AS name, contype AS type, convalidated AS validated,
          condeferrable AS deferrable, condeferred AS deferred,
          pg_get_constraintdef(oid,false) AS definition
   FROM pg_constraint WHERE conrelid='public.payment_attempts'::regclass) v),
 'indexes', (SELECT COALESCE(json_agg(v ORDER BY v.name),'[]'::json) FROM (
   SELECT i.indexrelid::regclass::text AS name, i.indisunique AS unique_index,
          i.indisprimary AS primary_index, i.indisvalid AS valid, i.indisready AS ready,
          pg_get_indexdef(i.indexrelid) AS definition
   FROM pg_index i WHERE i.indrelid='public.payment_attempts'::regclass) v),
 'triggers', (SELECT COALESCE(json_agg(pg_get_triggerdef(oid,false) ORDER BY tgname),'[]'::json)
              FROM pg_trigger WHERE tgrelid='public.payment_attempts'::regclass AND NOT tgisinternal),
 'rules', (SELECT COALESCE(json_agg(pg_get_ruledef(oid,false) ORDER BY rulename),'[]'::json)
           FROM pg_rewrite WHERE ev_class='public.payment_attempts'::regclass),
 'policies', (SELECT COALESCE(json_agg(polname ORDER BY polname),'[]'::json)
              FROM pg_policy WHERE polrelid='public.payment_attempts'::regclass),
 'uuid_function', (SELECT json_build_object('signature',p.oid::regprocedure::text,
      'result',pg_get_function_result(p.oid),'language',l.lanname,'source',p.prosrc,
      'binary',p.probin,'security_definer',p.prosecdef,'volatility',p.provolatile,'config',p.proconfig)
    FROM pg_proc p JOIN pg_language l ON l.oid=p.prolang
    WHERE p.oid='pg_catalog.gen_random_uuid()'::regprocedure),
 'required_extensions', (SELECT COALESCE(json_agg(v ORDER BY v.name),'[]'::json) FROM (
   SELECT e.extname AS name, e.extversion AS version, n.nspname AS schema
   FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace WHERE e.oid IN (
     SELECT refobjid FROM pg_depend WHERE refclassid='pg_extension'::regclass AND deptype='e'
     AND ((classid='pg_proc'::regclass AND objid='pg_catalog.gen_random_uuid()'::regprocedure)
       OR (classid='pg_class'::regclass AND objid='public.payment_attempts'::regclass)))) v)
);
COMMIT;
"""


def schema_snapshot(raw):
    require(len(raw.encode()) <= 131072, 'Excessive fixed-schema metadata')
    value = json.loads(raw)
    require(set(value) == {'format', 'server_major', 'table', 'columns', 'constraints', 'indexes',
                          'triggers', 'rules', 'policies', 'uuid_function', 'required_extensions'}, 'Invalid fixed-schema snapshot')
    require(value['format'] == 1 and value['server_major'] == 17
            and value['table']['schema'] == 'public' and value['table']['name'] == 'payment_attempts'
            and value['table']['kind'] == 'r' and not value['table']['row_security']
            and not value['table']['force_row_security'], 'Unsupported demonstration table')
    require(all(isinstance(value[k], list) for k in ('columns', 'constraints', 'indexes', 'triggers', 'rules', 'policies', 'required_extensions'))
            and value['columns'] and value['constraints'] and value['indexes']
            and not value['triggers'] and not value['rules'] and not value['policies']
            and value['uuid_function']['signature'] == 'gen_random_uuid()', 'Unsupported fixed-schema objects')
    return value


def schema_digest(value):
    schema_snapshot(json.dumps(value))
    return digest(json.dumps(value, sort_keys=True, separators=(',', ':')).encode())


def validate_demo_contract(contract):
    require(re.fullmatch(r'[0-9a-f]{64}', contract.get('live_schema_sha256', '')), 'Missing reviewed live schema checksum')
    require(contract.get('required_checks') == list(DEMO_CHECKS), 'All fixed-schema compatibility checks are required')
    dependencies = contract.get('dependency_sha256')
    require(isinstance(dependencies, dict) and set(dependencies) == set(CHECKER_DEPENDENCIES)
            and all(isinstance(v, str) and re.fullmatch(r'[0-9a-f]{64}', v) for v in dependencies.values()),
            'Missing reviewed checker dependencies')


def validate_baseline(baseline, policy, policy_sha):
    approval = baseline.get('approval') or {}
    require(baseline.get('schema_version') == 1 and baseline.get('policy_sha256') == policy_sha
            and baseline.get('baseline_release_id') == policy['contract']['baseline_release_id']
            and all(isinstance(approval.get(k), str) and approval[k].strip() for k in ('reviewer', 'review_reference')),
            'Missing approved EC2 baseline')
    require(schema_digest(baseline['live_schema']) == policy['contract']['live_schema_sha256'],
            'EC2 baseline differs from the approved schema contract')


def require(value, message):
    if not value:
        raise ValueError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    # Normalize omitted/null/empty Compose defaults across serializers. Explicit
    # nonempty fields remain in the contract and consequently cannot change.
    if isinstance(value, dict):
        normalized = {k: canonical(v) for k, v in value.items()}
        return {k: v for k, v in normalized.items() if v is not None and v != {} and v != []}
    if isinstance(value, list):
        return [canonical(v) for v in value]
    return value


def json_hash(value):
    return digest(json.dumps(canonical(value), sort_keys=True, separators=(',', ':')).encode())


def contract_facts(directory, config):
    db = json.loads(json.dumps(config['services']['db']))
    for key in ('POSTGRES_USER', 'POSTGRES_DB', 'POSTGRES_PASSWORD'):
        require(key in db['environment'], 'Database environment missing')
        db['environment'][key] = '$' + key
    db['platform'] = db.get('platform', 'linux/amd64')
    # Compose 5 serializes these literal container variables with doubled dollars;
    # 2.40.3 emits single dollars. Normalize only this known demonstration probe.
    health = db.get('healthcheck') or {}
    if health.get('test') == ['CMD-SHELL', 'pg_isready -U "$$POSTGRES_USER" -d "$$POSTGRES_DB"']:
        health['test'] = ['CMD-SHELL', 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"']
    for mount in db['volumes']:
        if mount['target'] == '/docker-entrypoint-initdb.d/001-schema.sql':
            mount['source'] = '$PACKAGED_SCHEMA'
    with (directory / 'db/schema.sql').open('rb') as stream:
        h = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return {'schema_sha256': h.hexdigest(), 'postgres_image': db['image'],
            'database_service_sha256': json_hash(db), 'networks_sha256': json_hash(config.get('networks') or {}),
            'volumes_sha256': json_hash(config['volumes'])}


def secure(path, directory=False, mode=None):
    info = path.lstat()
    require(info.st_uid == 0 and not info.st_mode & 0o022
            and (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)), 'Unsafe root-owned deployment path')
    require(mode is None or stat.S_IMODE(info.st_mode) == mode, 'Unsafe deployment file permissions')


def read(path, limit=262144):
    secure(path)
    with path.open('rb') as stream:
        data = stream.read(limit + 1)
    require(len(data) <= limit, 'Excessive deployment metadata')
    return data


def aws(*args):
    result = subprocess.run(['aws', *args, '--region', 'us-east-1', '--output', 'json',
                             '--cli-connect-timeout', '5', '--cli-read-timeout', '20'],
                            env=ENV, capture_output=True, text=True, timeout=60)
    require(result.returncode == 0, 'Instance-role AWS request failed; output withheld')
    return json.loads(result.stdout)


def validate_request(request):
    require(request.get('repository') == 'Batsy1857/AsaanPay' and request.get('branch') == 'feature/aws-cicd-devsecops', 'Wrong deployment origin')
    require(re.fullmatch(r'[0-9a-f]{32}', request.get('nonce', '')), 'Invalid invocation nonce')
    for key in ('marker_sha256', 'policy_sha256', 'script_sha256'):
        require(re.fullmatch(r'[0-9a-f]{64}', request.get(key, '')), 'Invalid trusted checksum')
    for key in ('workflow_id', 'run_number'):
        require(type(request.get(key)) is int and 0 < request[key] < 10**15, 'Invalid workflow order')
    expected = request['expected']
    require(re.fullmatch(r'[0-9a-f]{40}', expected.get('commit', '')), 'Invalid commit')
    require(re.fullmatch(r'[1-9][0-9]*', expected.get('run_id', ''))
            and re.fullmatch(r'[1-9][0-9]*', expected.get('run_attempt', '')), 'Invalid run identity')
    require(re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', expected.get('version', '')), 'Invalid version')
    identity = '{version}-g{commit}-r{run_id}-a{run_attempt}'.format(**expected)
    require(expected.get('release_id') == identity and len(identity) <= 128, 'Release identity mismatch')


def verify_instance():
    opener = build_opener(ProxyHandler({}))
    token_request = Request('http://169.254.169.254/latest/api/token', method='PUT',
                            headers={'X-aws-ec2-metadata-token-ttl-seconds': '60'})
    with opener.open(token_request, timeout=3) as response:
        token = response.read(4096).decode()
    request = Request('http://169.254.169.254/latest/dynamic/instance-identity/document',
                      headers={'X-aws-ec2-metadata-token': token})
    with opener.open(request, timeout=3) as response:
        identity = json.loads(response.read(16384))
    require(identity.get('accountId') == ACCOUNT and identity.get('region') == 'us-east-1'
            and identity.get('instanceId') == INSTANCE, 'Wrong EC2 instance identity')
    caller = aws('sts', 'get-caller-identity')
    require(caller.get('Account') == ACCOUNT and caller.get('Arn') ==
            'arn:aws:sts::' + ACCOUNT + ':assumed-role/AsaanPay-EC2-Role/' + INSTANCE, 'Unexpected EC2 instance role')


def install(expected_sha):
    folder = BASE / 'deployers'
    folder.mkdir(mode=0o700, exist_ok=True)
    secure(folder, directory=True)
    target = folder / (expected_sha + '.py')
    if target.exists() or target.is_symlink():
        require(digest(read(target)) == expected_sha, 'Installed deployer checksum mismatch')
        secure(target, mode=0o600)
        return target
    common = ('--bucket', 'asaanpay-releases', '--expected-bucket-owner', ACCOUNT,
              '--key', 'releases/deployment-tools/' + expected_sha + '/deploy_ec2.py')
    info = aws('s3api', 'head-object', *common)
    size, etag = info.get('ContentLength'), info.get('ETag', '')
    require(type(size) is int and 0 < size <= 262144 and re.fullmatch(r'"[0-9a-fA-F]+(?:-[0-9]+)?"', etag), 'Unsafe deployer object')
    fd, name = tempfile.mkstemp(dir=folder, prefix='.install-')
    os.close(fd)
    temporary = Path(name)
    try:
        aws('s3api', 'get-object', *common, '--if-match', etag, '--range', f'bytes=0-{size - 1}', str(temporary))
        require(temporary.stat().st_size == size and digest(read(temporary)) == expected_sha, 'Downloaded deployer checksum mismatch')
        with temporary.open('rb') as stream:
            os.fsync(stream.fileno())
        os.rename(temporary, target)
        directory_fd = os.open(str(folder), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()
    return target


def check_policy(request, policy):
    require(policy.get('schema_version') == 1 and policy.get('commissioned') is True
            and policy.get('approval', {}).get('reviewer') and policy['approval'].get('review_reference'), 'Host policy is not approved')
    contract, evidence = policy['contract'], request['evidence']
    validate_demo_contract(contract)
    require(contract.get('no_database_changes') is True, 'Database changes are forbidden')
    require(evidence.get('schema_version') == 1 and evidence.get('passed') is True
            and evidence.get('contract_id') == contract['id'] and evidence.get('policy_sha256') == request['policy_sha256']
            and evidence.get('release_id') == request['expected']['release_id'] and evidence.get('image_id') == request['image_id']
            and evidence.get('live_schema_sha256') == contract['live_schema_sha256']
            and evidence.get('checks') == {key: True for key in contract['required_checks']}
            and all(v is True for v in evidence['checks'].values()), 'Missing exact CI compatibility evidence')
    require(all(contract.get(k) == v for k, v in request['facts'].items()) and len(request['facts']) == 5,
            'Candidate is outside the approved host contract')


def check_order(request, current, order):
    require(order.get('workflow_id') == request['workflow_id'], 'CI workflow identity changed')
    for key in ('run_number', 'run_attempt'):
        require(type(order.get(key)) is int and order[key] >= 0, 'Invalid host deployment order')
    require(order.get('release_id') == (current or {}).get('release_id'), 'Host state/order mismatch; reconcile manually')
    previous = (order['run_number'], order['run_attempt'])
    candidate = (request['run_number'], int(request['expected']['run_attempt']))
    if current:
        identity = re.fullmatch(r'(.+)-g([0-9a-f]{40})-r([1-9][0-9]*)-a([1-9][0-9]*)', order['release_id'])
        require(identity and previous[0] > 0 and int(identity[4]) == previous[1], 'Ambiguous recorded run history')
        if candidate[0] == previous[0]:
            require((request['expected']['version'], request['expected']['commit'], request['expected']['run_id'])
                    == identity.group(1, 2, 3), 'Rerun changed its original release provenance')
        else:
            require(request['expected']['run_id'] != identity[3], 'Run ID reused with a different workflow sequence')
    else:
        require(previous == (0, 0), 'Initial adoption requires an explicitly reviewed zero-order baseline')
    require(candidate > previous or (candidate == previous and order['release_id'] == request['expected']['release_id']),
            'Stale or inconsistent CI deployment refused')


def ordered_deployer(module, request, policy, baseline):
    class OrderedDeployer(module.Deployer):
        def fixed_schema_preflight(self):
            value = self.docker('exec', '--interactive', baseline['database_container_id'], 'sh', '-c',
                                'exec psql -X -q -A -t -v ON_ERROR_STOP=1 -h /var/run/postgresql -p 5432 -U "$POSTGRES_USER" -d "$POSTGRES_DB"',
                                input_text=SCHEMA_SQL, timeout=15)
            require(schema_digest(schema_snapshot(value)) == policy['contract']['live_schema_sha256'],
                    'Live database differs from the approved fixed schema')

        def start_api(self, directory, manifest):
            # Recheck after potentially slow image loading, immediately before
            # M7E.5A performs its API-only Compose operation.
            self.fixed_schema_preflight()
            super().start_api(directory, manifest)

        def _deploy(self, release_id, rollback_safe_from, resume_interrupted, operations_stopped):
            check_policy(request, policy)
            validate_baseline(baseline, policy, request['policy_sha256'])
            state = self.read_state()
            require(not state['in_progress'], 'Interrupted deployment requires separate manual recovery')
            order_path = self.base / 'deployment-order.json'
            if order_path.exists() or order_path.is_symlink():
                secure(order_path, mode=0o600)
                order = json.loads(read(order_path))
            else:
                order = baseline['order']
            check_order(request, state['current'], order)
            directory, manifest = self.stage(release_id)
            require(module.validate_manifest(manifest) == request['expected']
                    and manifest['image_id'] == request['image_id']
                    and module.digest(directory / 'SHA256SUMS') == request['marker_sha256'], 'Host package differs from verified publication')
            config = self.configuration(directory, manifest)
            require(contract_facts(directory, config) == request['facts'], 'Packaged database contract changed')
            require(self.database(config) == baseline['database_container_id'], 'Commissioned database container changed')
            self.network_snapshot = baseline['network']
            self.network(baseline['database_container_id'])
            volume = json.loads(self.docker('volume', 'inspect', module.VOLUME))[0]
            require({k: volume.get(k) for k in ('Name', 'Driver', 'Options', 'Mountpoint', 'CreatedAt')} == baseline['volume'],
                    'Commissioned PostgreSQL volume changed')
            self.fixed_schema_preflight()
            # This is an explicit, commissioned policy authorization backed by
            # candidate-specific CI compatibility tests, NOT inference from SQL.
            source = (state['current'] or {}).get('release_id')
            super()._deploy(release_id, source if source != release_id else None, False, False)
            verified = self.read_state()
            require(verified['in_progress'] is None and verified['current']['release_id'] == release_id
                    and verified['current']['hashes']['SHA256SUMS'] == request['marker_sha256'], 'Final release state not verified')
            # A crash between base state and order writes fails closed next time:
            # order/state IDs must agree. Never silently repair that mismatch.
            module.atomic_state(order_path, {'workflow_id': request['workflow_id'], 'run_number': request['run_number'],
                                            'run_attempt': int(request['expected']['run_attempt']), 'release_id': release_id})
    return OrderedDeployer(BASE)


def execute(request):
    validate_request(request)
    require(os.geteuid() == 0, 'SSM bootstrap requires root')
    verify_instance()
    for path in (Path('/opt'), BASE, BASE / 'config'):
        secure(path, directory=True)
    policy_path, baseline_path = BASE / 'config/deployment-policy.json', BASE / 'config/deployment-baseline.json'
    for path in (policy_path, baseline_path):
        secure(path, mode=0o600)
    raw_policy = read(policy_path)
    require(digest(raw_policy) == request['policy_sha256'], 'CI policy differs from commissioned host policy')
    policy, baseline = json.loads(raw_policy), json.loads(read(baseline_path))
    check_policy(request, policy)
    validate_baseline(baseline, policy, request['policy_sha256'])
    lock_fd = os.open(str(BASE / '.deploy-bootstrap.lock'), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        require(stat.S_ISREG(os.fstat(lock_fd).st_mode), 'Unsafe bootstrap lock')
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = install(request['script_sha256'])
        sys.dont_write_bytecode = True
        spec = importlib.util.spec_from_file_location('verified_deployer', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        folder = Path('/run/asaanpay-deploy')
        folder.mkdir(mode=0o700, exist_ok=True)
        secure(folder, directory=True)
        receipt = folder / (request['nonce'] + '.json')
        require(not receipt.exists() and not receipt.is_symlink(), 'Invocation receipt already exists')
        ordered_deployer(module, request, policy, baseline).deploy(request['expected']['release_id'])
        module.atomic_state(receipt, {'nonce': request['nonce'], 'release_id': request['expected']['release_id'],
                                     'marker_sha256': request['marker_sha256']})
    finally:
        os.close(lock_fd)


if __name__ == '__main__':
    try:
        raw = base64.b64decode(sys.argv[1], validate=True)
        require(len(raw) <= 32768, 'Excessive deployment request')
        execute(json.loads(raw))
    except BaseException:
        # SSM output and systemd diagnostics must never reveal credentials.
        print('Deployment bootstrap failed; no success receipt issued. Inspect root-owned state and journal.', file=sys.stderr)
        raise SystemExit(1) from None
