"""Host-side workflow and Compose rendering checks; no AWS or containers."""
import json
import os
import subprocess
import tempfile
from pathlib import Path
import re
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts import deploy_pipeline as pipeline
from scripts import deploy_ec2 as deploy
sys.path.insert(0, str(pipeline.ROOT / 'tests'))
from test_deploy_ec2 import fixture, compose_config

GUARD = "${{ needs.publish-release.result == 'success' && github.event_name == 'push' && github.repository == 'Batsy1857/AsaanPay' && github.ref == 'refs/heads/feature/aws-cicd-devsecops' }}"


def checked_guard(job):
    # Exact supported expression, not substring presence: no OR/always()/comment
    # can smuggle an unconditional branch around the required predicates.
    values = re.findall(r'^    if: (.+)$', job, re.MULTILINE)
    if values != [GUARD]:
        raise ValueError('Deployment guard differs from the reviewed conjunction')
    return values[0]


class WorkflowTests(unittest.TestCase):
    def test_job_guards_permissions_pins_and_same_run_output_chain(self):
        text = (pipeline.ROOT / '.github/workflows/ci.yml').read_text()
        job = text.split('\n  deploy-production:\n', 1)[1]
        checked_guard(job)
        concurrency = re.search(r'^    concurrency:\n((?:      .+\n)+)', job, re.MULTILINE).group(1)
        self.assertEqual(dict(line.strip().split(': ',1) for line in concurrency.splitlines()),
                         {'group':'asaanpay-production-deployment','queue':'max','cancel-in-progress':'false'})
        permissions = re.search(r'^    permissions:\n((?:      .+\n)+)', job, re.MULTILINE).group(1)
        self.assertEqual(dict(line.strip().split(': ',1) for line in permissions.splitlines()),
                         {'contents':'read','actions':'read','id-token':'write'})
        for snippet in ('needs: [publish-release]', "needs.publish-release.result == 'success'", "github.event_name == 'push'",
                        "github.repository == 'Batsy1857/AsaanPay'", "github.ref == 'refs/heads/feature/aws-cicd-devsecops'",
                        'cancel-in-progress: false', 'PRODUCTION_DEPLOYMENT_ENABLED: "true"',
                        'PUBLISHED_RELEASE_ID: ${{ needs.publish-release.outputs.release-id }}',
                        'PUBLICATION_SHA256: ${{ needs.publish-release.outputs.marker-sha256 }}',
                        'name: asaanpay-release-${{ needs.publish-release.outputs.release-id }}', 'digest-mismatch: error',
                        'contents: read\n      actions: read\n      id-token: write'):
            self.assertIn(snippet, job)
        self.assertNotIn('workflow_dispatch:', text)
        self.assertNotIn('environment:', job)
        self.assertIn('release-id: ${{ steps.build.outputs.release-id }}', text)
        self.assertIn('release-id: ${{ steps.publication-proof.outputs.release-id }}', text)
        self.assertIn('BUILT_RELEASE_ID: ${{ needs.build-release.outputs.release-id }}', text)
        actions = re.findall(r'uses: (\S+)', job)
        self.assertEqual(len(actions), 4)
        self.assertTrue(all(re.fullmatch(r'[a-zA-Z0-9_/-]+@[0-9a-f]{40}', action) for action in actions))
        for step in job.split('\n      - name:')[1:]:
            if any(operation in step for operation in ('configure-aws-credentials@', 'deploy_pipeline.py authorize', 'deploy_pipeline.py execute')):
                self.assertEqual(re.findall(r'^        if: (.+)$',step,re.MULTILINE),
                                 ["${{ env.PRODUCTION_DEPLOYMENT_ENABLED == 'true' }}"])
        policy = json.loads((pipeline.ROOT / '.github/deployment-policy.json').read_text())
        validated, _ = pipeline.read_policy(pipeline.ROOT / '.github/deployment-policy.json', commissioned=True)
        self.assertEqual(policy, validated)
        self.assertIs(policy['commissioned'], True)
        self.assertEqual(policy['approval'], {'reviewer': 'Sadiq', 'review_reference': 'ASAANPAY-M7E5C2-20261122'})
        contract = policy['contract']
        self.assertEqual(contract['checker_sha256'], deploy.digest(pipeline.ROOT / 'scripts/demo_compatibility.py'))
        for name, checksum in contract['dependency_sha256'].items():
            self.assertEqual(deploy.digest(pipeline.ROOT / name), checksum)

    def test_contract_renderer_is_pinned_before_authorization(self):
        text = (pipeline.ROOT / '.github/workflows/ci.yml').read_text()
        job = text.split('\n  deploy-production:\n', 1)[1]
        steps = job.split('\n      - name:')[1:]
        pin = next(step for step in steps if step.startswith(' Pin Compose to the reviewed EC2 renderer\n'))
        self.assertNotIn('        if:', pin)
        self.assertIn('https://github.com/docker/compose/releases/download/v2.40.3/docker-compose-linux-x86_64', pin)
        self.assertIn('dba9d98e1ba5bfe11d88c99b9bd32fc4a0624a30fafe68eea34d61a3e42fd372', pin)
        self.assertLess(pin.index('sha256sum --check --strict'), pin.index('sudo install'))
        self.assertIn('/usr/local/lib/docker/cli-plugins/docker-compose', pin)
        self.assertIn('test "$(docker compose version --short)" = \'2.40.3\'', pin)
        self.assertIn('test "$(env -i PATH="$PATH" docker compose version --short)" = \'2.40.3\'', pin)
        self.assertLess(job.index('Pin Compose to the reviewed EC2 renderer'),
                        job.index('deploy_pipeline.py authorize'))


    def test_trivially_true_or_commented_guard_is_rejected(self):
        for bad in ('${{ true }}', GUARD.replace('${{ ', '${{ true || '),
                    GUARD.replace("needs.publish-release.result == 'success' && ", ''),
                    '${{ always() }} # ' + GUARD):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                checked_guard('    if: ' + bad + '\n')

    def test_approved_guard_rejects_noneligible_contexts(self):
        checked_guard('    if: ' + GUARD + '\n')
        terms = GUARD[4:-3].split(' && ')
        pairs = [term.split(' == ') for term in terms]
        approved = {key: value.strip("'") for key,value in pairs}
        evaluate = lambda context: all(context.get(key)==value.strip("'") for key,value in pairs)
        self.assertTrue(evaluate(approved))
        for key in approved:
            with self.subTest(key=key):
                self.assertFalse(evaluate({**approved,key:'not-approved'}))


class ComposeRenderingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.directory = root / 'package'
        self.manifest = fixture(self.directory)
        self.subject = deploy.Deployer(root)

    def test_live_compose_parser_accepts_projection_without_daemon(self):
        # This is a required host-side check: the development image has no Docker CLI.
        config = compose_config(self.directory, self.manifest)
        config['services']['api']['environment']['POSTGRES_PASSWORD'] = 'literal${DO_NOT_EXPAND}'
        source = self.subject.api_configuration(config, 'verified-network-id')
        command = ['docker', 'compose', '--env-file', '/dev/null', '-f', '-', 'config', '--format', 'json']
        result = subprocess.run(command, input=source, capture_output=True, text=True, timeout=15,
                                env={'PATH': os.environ['PATH']})
        self.assertEqual(result.returncode, 0, 'Compose config failed; output withheld')
        normalized = json.loads(result.stdout)
        self.assertEqual(set(normalized['services']), {'api'})
        self.assertTrue(normalized['networks']['default']['external'])
        # Compose 5 re-escapes dollars when serializing config; 2.40.3 emits
        # resolved values. Production preflight pins the reviewed 2.40.3 parser.
        self.assertIn(normalized['services']['api']['environment']['POSTGRES_PASSWORD'],
                      ('literal${DO_NOT_EXPAND}', 'literal$${DO_NOT_EXPAND}'))


if __name__ == '__main__':
    unittest.main()
