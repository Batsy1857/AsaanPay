"""Benchmark accounting/validation tests; fixtures here are NOT experiment data."""

from copy import deepcopy
import io
import json
from pathlib import Path
import statistics
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from uuid import uuid4

from scripts import benchmark as b

TRACES = json.loads((Path(__file__).parent / 'fixtures/timing_traces.json').read_text())['traces']


def observation(profile='balanced', mode='concurrent'):
    trial = next(row for row in b.schedule(0, 1, 'test-only-') if row['profile'] == profile and row['mode'] == mode)
    payload = {key: trial[key] for key in ('amount_pkr', 'card_preset', 'billing_country')}
    payload.update(execution_mode=mode, workload_profile=profile)
    body = dict(id=str(uuid4()), state='SUCCEEDED', decision='SUCCEEDED', terminal=True,
                issuer_result='APPROVED', risk_result='CLEAR', rule_set_version='sandbox_v1',
                request=payload, decision_reason='The issuer approved and risk screening is clear.', error_reason=None,
                timing_trace=deepcopy(TRACES[f'{profile}_{mode}']))
    row = {**trial, 'http_status': 201, 'http_ms': 1000.0, 'errors': []}
    return row, body, payload


class BenchmarkTests(unittest.TestCase):
    def test_default_schedule_has_balanced_identical_inputs_and_unique_keys(self):
        rows = list(b.schedule(5, 50, 'asaanpay-bench-test-'))
        self.assertEqual(len(rows), 330)
        self.assertEqual(sum(row['phase'] == 'warmup' for row in rows), 30)
        self.assertEqual(len({row['idempotency_key'] for row in rows}), 330)
        self.assertTrue(all(len(row['idempotency_key']) <= 128 for row in rows))
        self.assertTrue(all(row['phase'] == 'warmup' for row in rows[:30]))
        for first, second in zip(rows[::2], rows[1::2]):
            self.assertEqual(first['pair_id'], second['pair_id'])
            self.assertNotEqual(first['mode'], second['mode'])
            for key in ('amount_pkr', 'card_preset', 'billing_country', 'expected_decision'):
                self.assertEqual(first[key], second[key])
        for profile in b.PROFILES:
            for mode in b.MODES:
                group = [r for r in rows if r['phase'] == 'measured' and r['profile'] == profile and r['mode'] == mode]
                self.assertEqual(len(group), 50)
                self.assertEqual(sum(r['order_in_pair'] == 1 for r in group), 25)
                self.assertEqual(set(b.Counter(r['case_id'] for r in group).values()), {5})

    def test_known_percentiles_and_sample_deviation(self):
        result = b.describe(list(range(1, 51)))
        self.assertEqual(result, dict(n=50, mean=25.5, median=25.5, p95=48, min=1, max=50,
                                      stdev=statistics.stdev(range(1, 51))))
        self.assertEqual(b.describe([3])['p95'], 3)
        self.assertIsNone(b.describe([3])['stdev'])
        self.assertIsNone(b.describe([])['median'])

    def test_all_six_captured_trace_contracts_validate(self):
        for profile in b.PROFILES:
            for mode in b.MODES:
                row, body, payload = observation(profile, mode)
                b.validate_trial(row, body, payload, set())
                self.assertEqual(row['errors'], [])
                self.assertTrue(row['decision_correct'])

    def test_malformed_and_inconsistent_trace_is_rejected(self):
        changes = (
            lambda t: t.update(orchestration_ms=float('nan')),
            lambda t: t.update(overlap_ms=float('inf')),
            lambda t: t.update(decision_ms=True),
            lambda t: t.update(decision_ms=-1),
            lambda t: t.update(decision_ms='250'),
            lambda t: t.update(overlap_ms=0),
            lambda t: t.update(branch_timeout_ms=1000),
            lambda t: t.update(artificial_delays=False),
            lambda t: t['branches']['issuer'].update(duration_ms=42),
            lambda t: t['branches']['risk'].update(start_ms=999),
            lambda t: t['branches']['risk'].update(artificial_delay_ms=400),
            lambda t: t['branches']['risk'].update(status='timeout', error_type='TimeoutError'),
            lambda t: t['branches'].pop('risk'),
        )
        for change in changes:
            row, body, payload = observation()
            change(body['timing_trace'])
            b.validate_trial(row, body, payload, set())
            self.assertTrue(row['errors'])

    def test_sequential_overlap_and_http_boundary_are_checked(self):
        row, body, payload = observation(mode='sequential')
        trace = body['timing_trace']
        trace['branches']['risk']['start_ms'] = trace['branches']['issuer']['start_ms']
        trace['branches']['risk']['duration_ms'] = trace['branches']['risk']['end_ms'] - trace['branches']['risk']['start_ms']
        trace['overlap_ms'] = trace['branches']['issuer']['duration_ms']
        row['http_ms'] = 1
        b.validate_trial(row, body, payload, set())
        self.assertIn('sequential_overlap_or_order', row['errors'])
        self.assertIn('orchestration_exceeds_http', row['errors'])

    def test_sub_nanosecond_arithmetic_tolerance_is_documented_and_limited(self):
        row, body, payload = observation()
        body['timing_trace']['overlap_ms'] += b.EPSILON_MS / 2
        b.validate_trial(row, body, payload, set())
        self.assertEqual(row['errors'], [])

    def test_replay_duplicate_identity_and_wrong_business_outcome_are_failures(self):
        row, body, payload = observation()
        row['http_status'] = 200
        body.update(state='DECLINED', decision='DECLINED')
        b.validate_trial(row, body, payload, {body['id']})
        self.assertFalse(row['decision_correct'])
        self.assertIn('reused_attempt_id', row['errors'])
        self.assertTrue(any(e.startswith('unexpected_http_status') for e in row['errors']))

    def test_pair_explanations_must_match_and_missing_pair_is_not_consistency(self):
        rows = []
        for mode in b.MODES:
            row, body, payload = observation(mode=mode)
            body['decision_reason'] = mode
            b.validate_trial(row, body, payload, set())
            rows.append(row)
        b.check_pairs(rows)
        self.assertTrue(all(not row['valid'] and not row['pair_match'] for row in rows))
        row, body, payload = observation()
        b.validate_trial(row, body, payload, set())
        b.check_pairs([row])
        self.assertFalse(row['valid'])

    def test_errors_and_warmups_are_retained_but_not_normal_latency_samples(self):
        rows = []
        for mode in b.MODES:
            row, body, payload = observation(mode=mode)
            b.validate_trial(row, body, payload, set())
            rows.append(row)
        warmup = deepcopy(rows[0])
        warmup.update(phase='warmup', pair_id='warmup-only', http_ms=999999)
        rows.append(warmup)
        rows[1]['errors'].append('test_failure')
        b.check_pairs(rows)
        summary = b.summarize(rows, dict(trials_per_mode_profile=1, warmups_per_mode_profile=1))
        self.assertEqual(summary['status'], 'FAIL')
        self.assertEqual(summary['measured_errors'], 1)
        self.assertEqual(summary['warmup_errors'], 1)
        seq, con = [g for g in summary['groups'] if g['profile'] == 'balanced']
        self.assertEqual(seq['metrics']['http_ms']['mean'], 1000)
        self.assertEqual(con['metrics']['http_ms']['n'], 0)
        self.assertEqual(con['all_attempt_http_ms']['n'], 1)
        self.assertIsNone(summary['comparisons'][1]['median_reduction_percent']['http_ms'])

    def test_transport_timeout_has_elapsed_time_and_stops(self):
        class Broken:
            def open(self, *_args, **_kwargs):
                raise URLError(TimeoutError('test timeout'))
        trial = next(b.schedule(0, 1, 'test-'))
        row, body, payload, uncertain = b.post_trial(Broken(), 'http://localhost', 1, trial)
        self.assertTrue(uncertain)
        self.assertGreater(row['http_elapsed_ns'], 0)
        self.assertTrue(any('TimeoutError' in error or 'timeout' in error for error in row['errors']))
        self.assertIsNone(body)

    def test_uncertain_503_stops_run_and_preserves_failed_sample_outputs(self):
        class FakeOpener:
            calls = 0
            def open(self, *_args, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    response = io.BytesIO(b'{"status":"ok","database":"connected"}')
                    response.status = 200
                    return response
                raise HTTPError('http://localhost', 503, 'Unavailable', {}, io.BytesIO(b'{"detail":"unavailable"}'))
        opener = FakeOpener()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'result'
            # This transport-failure test must not depend on a source checkout:
            # the API image intentionally omits Compose and database schema files.
            source = Path(directory) / 'source'
            for name in ('scripts/benchmark.py', 'requirements.txt', 'compose.yaml', 'db/schema.sql'):
                path = source / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('# Test-only metadata fixture\n')
            with patch.object(b, 'ROOT', source), patch.object(b, 'build_opener', return_value=opener), \
                    patch('sys.stdout', new=io.StringIO()):
                code = b.main(['--trials', '2', '--warmups', '0', '--output', str(output)])
            self.assertEqual(code, 1)
            self.assertEqual(opener.calls, 2)  # health plus exactly one POST
            summary = json.loads((output / 'summary.json').read_text())
            self.assertEqual(summary['measured_attempts'], 1)
            self.assertEqual(summary['measured_errors'], 1)
            self.assertEqual(len(summary['failures']), 1)
            self.assertIn('503', (output / 'trials.csv').read_text())
            self.assertIn('**FAIL**', (output / 'report.md').read_text())
