"""Synthetic unit-test approvals only. Never installed or used as deployment policy."""
from pathlib import Path
from scripts import deploy_ec2 as deploy, ssm_deploy as host

ROOT = Path(__file__).resolve().parent.parent


def schema():
    return {'format':1, 'server_major':17, 'table':{'schema':'public','name':'payment_attempts','kind':'r',
            'row_security':False,'force_row_security':False},
            'columns':[{'name':'id','type':'uuid','not_null':True,'default_expression':'gen_random_uuid()'}],
            'constraints':[{'name':'payment_attempts_pkey','definition':'PRIMARY KEY (id)'}],
            'indexes':[{'name':'payment_attempts_pkey','definition':'fixture'}], 'triggers':[], 'rules':[], 'policies':[],
            'uuid_function':{'signature':'gen_random_uuid()'}, 'required_extensions':[]}


def policy(facts, release_id):
    return {'schema_version':1,'commissioned':True,'approval':{'reviewer':'TEST ONLY','review_reference':'unit-fixture'},
            'contract':{**facts,'id':'fixture-only','no_database_changes':True,'baseline_release_id':release_id,
                        'live_schema_sha256':host.schema_digest(schema()), 'required_checks':list(host.DEMO_CHECKS),
                        'checker_sha256':deploy.digest(ROOT/'scripts/demo_compatibility.py'),
                        'dependency_sha256':{name:deploy.digest(ROOT/name) for name in host.CHECKER_DEPENDENCIES}}}


def evidence(request, value):
    return {'schema_version':1,'passed':True,'release_id':request['expected']['release_id'],
            'image_id':request['image_id'],'policy_sha256':request['policy_sha256'],'contract_id':value['contract']['id'],
            'live_schema_sha256':value['contract']['live_schema_sha256'],'checks':{key:True for key in host.DEMO_CHECKS}}
