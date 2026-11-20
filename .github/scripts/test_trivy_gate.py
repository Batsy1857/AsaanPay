"""Scanner/DB execution failures must never produce policy approval."""

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class TrivyGateTests(unittest.TestCase):
    def test_db_and_scanner_failures_remove_stale_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scanner = root / 'trivy'
            for phase in ('database', 'scan'):
                with self.subTest(phase=phase):
                    scanner.write_text(f'''#!{sys.executable}
import pathlib
import sys
args = sys.argv[1:]
if args == ['--version']:
    print('Version: 0.69.3')
    sys.exit(0)
if '--download-db-only' in args:
    if {phase!r} == 'database':
        sys.exit(17)
    cache = pathlib.Path(args[args.index('--cache-dir') + 1]) / 'db'
    cache.mkdir()
    (cache / 'metadata.json').write_text('{{}}')
    sys.exit(0)
sys.exit(18)
''')
                    scanner.chmod(0o700)
                    output = root / 'output'
                    output.mkdir(exist_ok=True)
                    receipt = output / 'approved.json'
                    receipt.write_text('stale approval')
                    summary = output / 'summary.md'
                    summary.write_text('## Release image security gate: PASS\nStale successful scan')
                    result = subprocess.run(['bash', str(ROOT / '.github/scripts/trivy_gate.sh'),
                                             str(scanner), 'sha256:' + 'a' * 64, str(output)],
                                            capture_output=True, text=True, timeout=20)
                    self.assertEqual(result.returncode, 17 if phase == 'database' else 18)
                    self.assertFalse(receipt.exists())
                    self.assertTrue(summary.read_text().startswith('## Release image security gate: FAIL'))
                    self.assertIn('Blocking findings: unknown', summary.read_text())
                    self.assertNotIn('Stale', summary.read_text())
