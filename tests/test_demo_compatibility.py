"""Checker regressions with mocked Docker/HTTP; real fixtures are separately exercised locally."""
from contextlib import contextmanager, redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import demo_compatibility as checker, release, ssm_deploy as host, deploy_ec2 as deploy
from test_deploy_ec2 import fixture, compose_config
import demo_fixtures


class CompatibilityTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory=Path(temporary.name)/'package'
        self.manifest=fixture(self.directory)
        self.config=compose_config(self.directory,self.manifest)
        self.policy=demo_fixtures.policy(host.contract_facts(self.directory,self.config),self.manifest['release_id'])
        self.policy_sha=host.digest(json.dumps(self.policy).encode())
        self.records={}
        self.mode=None
        self.catalog_reads=0
        self.loaded=0
        self.cleaned=False
        labels=dict(zip(('org.opencontainers.image.version','org.opencontainers.image.revision','io.asaanpay.release-id',
                         'io.asaanpay.run-id','io.asaanpay.run-attempt'),
                        (self.manifest[k] for k in ('version','commit','release_id','run_id','run_attempt'))))
        self.image={'Id':self.manifest['image_id'],'Os':'linux','Architecture':'amd64','Config':{'User':'10001:10001','Labels':labels}}
        for name,side_effect in (('docker',self.docker),('query',self.query),('http',self.http),('stack',self.stack)):
            p=patch.object(release,name,side_effect=side_effect)
            setattr(self,name+'_mock',p.start())
            self.addCleanup(p.stop)
        p=patch.object(release,'digest',deploy.digest)  # CI helper parity under Python 3.9.
        p.start()
        self.addCleanup(p.stop)

    def docker(self,*args,**kwargs):
        if args[0]=='compose':
            return json.dumps(self.config)
        if args[0]=='load':
            self.loaded+=1
            return ''
        if args[:2]==('image','inspect'):
            return json.dumps([self.image])
        if args[0]=='run':
            return json.dumps(release.validate_manifest(self.manifest))
        if args[:2]==('exec','fixture-db'):
            self.assertIn('-X',args)
            self.assertIn('-q',args)
            self.assertEqual(args[-1],host.SCHEMA_SQL)
            self.assertEqual(kwargs['timeout'],15)
            self.catalog_reads+=1
            value=demo_fixtures.schema()
            if self.mode=='schema' and self.catalog_reads>1:
                value['columns'][0]['not_null']=False
            return json.dumps(value)
        self.fail('Unexpected Docker operation: '+repr(args))

    @contextmanager
    def stack(self,image,**kwargs):
        self.assertEqual(image,self.manifest['image_id'])
        self.assertEqual(kwargs['db_image'],self.policy['contract']['postgres_image'])
        self.assertEqual(kwargs['schema'],(self.directory/'db/schema.sql').resolve())
        try:
            kwargs['before_api']('fixture-db')
            if self.mode=='startup':
                raise ValueError('Startup failed')
            row=self.records[checker.UNFINISHED_ID]
            if self.mode!='recovery':
                row.update(state='ERROR',decision=None,decision_reason=None,finished_at='recovered-at',
                           error_reason='PROCESS_INTERRUPTED: previous execution was interrupted')
            if self.mode=='existing':
                self.records[checker.EXISTING_ID]['amount_pkr']=999
            yield 'http://127.0.0.1:12345','fixture-db','fixture-api'
        finally:
            self.cleaned=True

    def query(self,db,sql):
        self.assertEqual(db,'fixture-db')
        if sql==checker.SEED_SQL:
            for identity,state in ((checker.EXISTING_ID,'SUCCEEDED'),(checker.UNFINISHED_ID,'PROCESSING')):
                self.records[identity]={'id':identity,'state':state,'amount_pkr':100,'idempotency_key':identity,
                                       'decision':'SUCCEEDED' if state=='SUCCEEDED' else None,'decision_reason':None,
                                       'error_reason':None,'finished_at':'old-time' if state=='SUCCEEDED' else None}
            return ''
        if sql=='SELECT count(*) FROM payment_attempts':
            return str(len(self.records)+(1 if self.mode=='duplicate' else 0))
        if sql.startswith('SELECT row_to_json(p)'):
            identity=sql.split("'")[1]
            return json.dumps(self.records[identity])
        self.fail('Unexpected SQL')

    def http(self,base,path,**kwargs):
        if path=='/api/health':
            value={'status':'ok','database':'connected'}
        elif path=='/api/version':
            value={key:self.manifest[key] for key in ('version','commit')}
        elif path.startswith('/api/checkouts/'):
            row=self.records[path.rsplit('/',1)[1]]
            value={'id':row['id'],'state':row['state'],'terminal':row['state'] in ('SUCCEEDED','ERROR')}
        elif path=='/api/checkouts':
            identity='33333333-3333-4333-8333-333333333333'
            replay=identity in self.records
            self.records[identity]={'id':identity,'state':'SUCCEEDED','idempotency_key':kwargs['key'],
                                   'rule_set_version':'sandbox_v1','finished_at':'new-time'}
            return (200 if replay else 201),'application/json',json.dumps({'id':identity,'state':'SUCCEEDED','terminal':True}).encode()
        else:
            assets={'/':('text/html','id="checkout-form"'),'/static/index.html':('text/html','id="version-badge"'),
                    '/static/styles.css':('text/css','#version-badge'),'/static/core.js':('javascript','CheckoutClient'),
                    '/static/app.js':('javascript','checkout-form'),'/static/version.js':('javascript','/api/version')}
            mime,body=assets[path]
            return 200,mime,body.encode()
        return 200,'application/json',json.dumps(value).encode()

    def verify(self):
        with redirect_stdout(io.StringIO()):
            return checker.verify(self.directory,self.policy,self.policy_sha)

    def test_success_binds_every_executed_check_and_cleans_resources(self):
        evidence=self.verify()
        self.assertEqual(evidence['checks'],{key:True for key in host.DEMO_CHECKS})
        self.assertEqual(evidence['image_id'],self.manifest['image_id'])
        self.assertEqual(evidence['release_id'],self.manifest['release_id'])
        self.assertEqual(evidence['policy_sha256'],self.policy_sha)
        self.assertEqual(evidence['live_schema_sha256'],self.policy['contract']['live_schema_sha256'])
        self.assertEqual(len(self.records),3)
        self.assertTrue(self.cleaned)
        self.assertEqual(self.catalog_reads,2)

    def test_existing_record_change_is_rejected(self):
        self.mode='existing'
        with self.assertRaisesRegex(ValueError,'existing completed'):
            self.verify()
        self.assertTrue(self.cleaned)

    def test_startup_recovery_is_required(self):
        self.mode='recovery'
        with self.assertRaises(ValueError):
            self.verify()
        self.assertTrue(self.cleaned)

    def test_startup_failure_never_emits_evidence(self):
        self.mode='startup'
        with self.assertRaises(ValueError):
            self.verify()
        self.assertTrue(self.cleaned)

    def test_duplicate_checkout_is_rejected(self):
        self.mode='duplicate'
        with self.assertRaisesRegex(ValueError,'extra record'):
            self.verify()

    def test_schema_change_after_candidate_runs_is_rejected(self):
        self.mode='schema'
        with self.assertRaisesRegex(ValueError,'changed the database schema'):
            self.verify()

    def test_wrong_image_identity_stops_before_startup(self):
        self.image['Id']='sha256:'+'f'*64
        with self.assertRaisesRegex(ValueError,'Image ID changed'):
            self.verify()
        self.stack_mock.assert_not_called()

    def test_changed_packaged_schema_stops_before_loading(self):
        self.policy['contract']['schema_sha256']='f'*64
        with self.assertRaisesRegex(ValueError,'Packaged schema changed'):
            self.verify()
        self.assertEqual(self.loaded,0)

    def test_changed_database_configuration_stops_before_loading(self):
        self.policy['contract']['database_service_sha256']='f'*64
        with self.assertRaisesRegex(ValueError,'fixed database configuration'):
            self.verify()
        self.assertEqual(self.loaded,0)

    def test_unapproved_live_baseline_stops_before_candidate_startup(self):
        self.policy['contract']['live_schema_sha256']='f'*64
        with self.assertRaisesRegex(ValueError,'approved EC2 baseline'):
            self.verify()
        self.assertEqual(self.records,{})
        self.assertTrue(self.cleaned)

    def test_uncommissioned_policy_and_unreviewed_sources_fail_before_docker(self):
        original=deepcopy(self.policy)
        for change in ('commissioned','approval','checker','dependency','missing-dependency'):
            self.policy=deepcopy(original)
            if change=='commissioned': self.policy['commissioned']=False
            elif change=='approval': self.policy['approval']=None
            elif change=='checker': self.policy['contract']['checker_sha256']='f'*64
            elif change=='dependency': self.policy['contract']['dependency_sha256']['scripts/release.py']='f'*64
            else: self.policy['contract']['dependency_sha256'].pop('app/version.py')
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.verify()
        self.docker_mock.assert_not_called()

    def test_all_required_checks_must_be_requested(self):
        self.policy['contract']['required_checks']=['startup_health']
        with self.assertRaisesRegex(ValueError,'All fixed-schema'):
            self.verify()
        self.docker_mock.assert_not_called()


class SchemaTests(unittest.TestCase):
    def test_catalog_fingerprint_covers_columns_defaults_constraints_indexes_and_dependencies(self):
        original=demo_fixtures.schema()
        expected=host.schema_digest(original)
        for name in ('columns','constraints','indexes','required_extensions'):
            changed=deepcopy(original)
            changed[name].append({'unexpected':'object'})
            self.assertNotEqual(host.schema_digest(changed),expected)
        for name,value in (('type','text'),('not_null',False),('default_expression','NULL')):
            changed=deepcopy(original)
            changed['columns'][0][name]=value
            self.assertNotEqual(host.schema_digest(changed),expected)

    def test_missing_unsupported_or_excessive_schema_is_rejected(self):
        for raw in ('{}','null','x'*131073):
            with self.subTest(raw=raw[:20]), self.assertRaises((ValueError,TypeError)):
                host.schema_snapshot(raw)
        for key,bad in (('server_major',16),('triggers',['unexpected']),('policies',['unexpected'])):
            with self.subTest(key=key), self.assertRaises(ValueError):
                host.schema_snapshot(json.dumps({**demo_fixtures.schema(),key:bad}))

    def test_preflight_sql_has_read_only_transaction_and_bounds(self):
        self.assertTrue(host.SCHEMA_SQL.strip().startswith('BEGIN READ ONLY;'))
        self.assertIn("statement_timeout = '5s'",host.SCHEMA_SQL)
        self.assertIn("lock_timeout = '2s'",host.SCHEMA_SQL)
        for operation in ('INSERT INTO','UPDATE ','DELETE ','ALTER ','CREATE ','DROP '):
            self.assertNotIn(operation,host.SCHEMA_SQL.upper())


if __name__=='__main__':
    unittest.main()
