"""Fixed-schema demonstration checker. Only UUID-owned disposable containers are used.

No image builds or AWS calls. An uncommissioned policy never emits approval.
Fixtures below and every local verification dependency are checksum-bound.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
DEPENDENCIES = ('app/__init__.py', 'app/version.py', 'scripts/release.py', 'scripts/ssm_deploy.py')
EXISTING_ID = '11111111-1111-4111-8111-111111111111'
UNFINISHED_ID = '22222222-2222-4222-8222-222222222222'
SEED_SQL = """INSERT INTO payment_attempts
 (id,idempotency_key,request_fingerprint,amount_pkr,card_preset,billing_country,execution_mode,
  state,issuer_result,risk_result,decision,decision_reason,finished_at)
 VALUES ('11111111-1111-4111-8111-111111111111','demo-existing',repeat('a',64),100,'approved','PK',
 'concurrent','SUCCEEDED','APPROVED','CLEAR','SUCCEEDED','approved fixture',CURRENT_TIMESTAMP);
 INSERT INTO payment_attempts
 (id,idempotency_key,request_fingerprint,amount_pkr,card_preset,billing_country,execution_mode,state,processing_started_at)
 VALUES ('22222222-2222-4222-8222-222222222222','demo-unfinished',repeat('b',64),100,'approved','PK',
 'concurrent','PROCESSING',CURRENT_TIMESTAMP);"""


def require(value, message):
    if not value:
        raise ValueError(message)


def digest(path):
    with path.open('rb') as stream:
        h = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def reviewed_sources(policy):
    require(policy.get('schema_version') == 1 and policy.get('commissioned') is True, 'Demo policy is not commissioned')
    approval = policy.get('approval') or {}
    require(all(isinstance(approval.get(k), str) and approval[k].strip() for k in ('reviewer','review_reference')),
            'Missing reviewed policy approval')
    contract = policy['contract']
    require(contract.get('no_database_changes') is True, 'Database changes are forbidden')
    hashes = contract.get('dependency_sha256') or {}
    require(set(hashes) == set(DEPENDENCIES), 'Missing reviewed verification dependencies')
    require(not (ROOT / 'scripts/__init__.py').exists(), 'Unexpected verification package initializer')
    for name, expected in {**hashes, 'scripts/demo_compatibility.py': contract['checker_sha256']}.items():
        path = ROOT / name
        require(path.is_file() and not path.is_symlink() and not path.parent.is_symlink()
                and digest(path) == expected, 'Verification source changed: ' + name)


def snapshot(release, host, db):
    raw = release.docker('exec', db, 'psql', '-X', '-q', '-A', '-t', '-v', 'ON_ERROR_STOP=1',
                         '-h', '/var/run/postgresql', '-p', '5432', '-U', 'asaanpay_smoke',
                         '-d', 'asaanpay_smoke', '-c', host.SCHEMA_SQL, timeout=15)
    return host.schema_snapshot(raw)


def record(release, db, identity):
    # identity is one of the fixed fixture UUIDs above, never an external SQL input.
    require(identity in (EXISTING_ID, UNFINISHED_ID), 'Unexpected fixture identity')
    return json.loads(release.query(db, "SELECT row_to_json(p) FROM payment_attempts p WHERE id='" + identity + "'"))


def exercise_candidate(release, host, directory, manifest, contract):
    original = {}
    def seed(db):
        value = snapshot(release, host, db)
        require(host.schema_digest(value) == contract['live_schema_sha256'], 'Packaged schema differs from approved EC2 baseline')
        original['schema'] = value
        release.query(db, SEED_SQL)
        original['existing'] = record(release, db, EXISTING_ID)
        original['unfinished'] = record(release, db, UNFINISHED_ID)

    with release.stack(manifest['image_id'], schema=(directory / 'db/schema.sql').resolve(),
                       db_image=contract['postgres_image'], before_api=seed) as (base, db, _api):
        for identity in (EXISTING_ID, UNFINISHED_ID):
            status, _, body = release.http(base, '/api/checkouts/' + identity)
            response = json.loads(body)
            require(status == 200 and response['id'] == identity and response['terminal'] is True,
                    'Existing record is not accessible')
            require(response['state'] == ('SUCCEEDED' if identity == EXISTING_ID else 'ERROR'), 'Existing state mismatch')
        require(record(release, db, EXISTING_ID) == original['existing'], 'Candidate changed an existing completed payment')
        recovered = record(release, db, UNFINISHED_ID)
        mutable = {'state', 'decision', 'decision_reason', 'error_reason', 'finished_at'}
        require({k:v for k,v in recovered.items() if k not in mutable}
                == {k:v for k,v in original['unfinished'].items() if k not in mutable}
                and recovered['state'] == 'ERROR' and recovered['decision'] is None
                and recovered['decision_reason'] is None and recovered['finished_at'] is not None
                and recovered['error_reason'] == 'PROCESS_INTERRUPTED: previous execution was interrupted',
                'Startup recovery violated the demo contract')
        # Reuse the real health/version/assets/checkout/persistence/replay tests.
        release.exercise(base, db, release.validate_manifest(manifest), existing_rows=2)
        require(record(release, db, EXISTING_ID) == original['existing']
                and record(release, db, UNFINISHED_ID) == recovered, 'Checkout altered existing records')
        require(snapshot(release, host, db) == original['schema'], 'Candidate changed the database schema')


def verify(directory, policy, policy_sha):
    reviewed_sources(policy)  # Verify the import closure BEFORE executing its code.
    sys.path.insert(0, str(ROOT))
    from scripts import release, ssm_deploy as host
    contract = policy['contract']
    host.validate_demo_contract(contract)
    manifest = release.check_checksums(directory)
    require(not manifest['local_validation'] and not manifest['source_dirty'], 'Only clean CI candidates are eligible')
    require(digest(directory / 'db/schema.sql') == contract['schema_sha256'], 'Packaged schema changed')
    db_image = release.validate_deployment(directory, manifest)
    # Obtain the normalized model for the same five configuration facts used on EC2.
    import os
    environment = {'PATH': os.environ.get('PATH','/usr/bin:/bin'), 'POSTGRES_USER':'contract',
                   'POSTGRES_DB':'contract', 'POSTGRES_PASSWORD':'dummy-only', 'ASAANPAY_IMAGE':manifest['image']}
    config = json.loads(release.docker('compose', '--env-file', '/dev/null', '--project-directory', str(directory.resolve()),
                                      '-f', str((directory / 'compose.prod.yaml').resolve()), 'config', '--format', 'json', env=environment))
    require(db_image == contract['postgres_image'] and all(contract.get(k)==v for k,v in host.contract_facts(directory,config).items()),
            'Candidate changes the fixed database configuration')
    release.docker('load', '--input', str(directory / 'image.tar.gz'), timeout=600)
    release.check_image({'metadata':release.validate_manifest(manifest), 'image':manifest['image'], 'image_id':manifest['image_id']})
    exercise_candidate(release, host, directory, manifest, contract)
    # Only reached after successful assertions AND disposable resource cleanup.
    return {'schema_version':1, 'passed':True, 'release_id':manifest['release_id'], 'image_id':manifest['image_id'],
            'policy_sha256':policy_sha, 'contract_id':contract['id'], 'live_schema_sha256':contract['live_schema_sha256'],
            'checks':{name:True for name in host.DEMO_CHECKS}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('package','policy','output'):
        parser.add_argument('--'+name, type=Path, required=True)
    args = parser.parse_args()
    require(not args.output.exists() and not args.output.is_symlink(), 'Evidence output must be new')
    with args.policy.open('rb') as stream:
        raw = stream.read(65537)
    require(len(raw)<=65536, 'Excessive policy metadata')
    value = verify(args.package, json.loads(raw), hashlib.sha256(raw).hexdigest())
    with args.output.open('x') as stream:
        json.dump(value, stream, sort_keys=True)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('Fixed-schema compatibility failed; no passing evidence issued. Details withheld.', file=sys.stderr)
        raise SystemExit(1) from None
