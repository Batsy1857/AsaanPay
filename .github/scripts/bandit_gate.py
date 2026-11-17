"""Run the SAST gate without exposing source snippets or accepting partial scans."""

import json
from pathlib import Path
import re
import subprocess
import sys


def main():
    targets = ("app", "scripts")
    if any(not Path(target).is_dir() for target in targets):
        print("Bandit configuration error: run from the repository root.")
        return 2
    expected = {path.resolve() for target in targets for path in Path(target).rglob("*.py")}
    if not expected:
        print("Bandit configuration error: no Python source files found.")
        return 2

    result = subprocess.run(
        [sys.executable, "-m", "bandit", "-r", *targets, "-ll", "-ii", "-f", "json"],
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode not in (0, 1) or re.search(r"\b(ERROR|CRITICAL)\b", result.stderr):
        print(f"Bandit execution error (exit {result.returncode}); raw diagnostics withheld to avoid source disclosure.")
        return 2
    try:
        report = json.loads(result.stdout)
        findings, errors, metrics = report["results"], report["errors"], report["metrics"]
        if not isinstance(findings, list) or not isinstance(errors, list) or not isinstance(metrics, dict):
            raise ValueError("Invalid report structure")
        scanned = {Path(filename).resolve() for filename in metrics if filename != "_totals"}
    except (ValueError, KeyError, TypeError):
        print("Bandit execution error: missing or invalid JSON report.")
        return 2
    if errors or expected - scanned:
        print(f"Bandit scan incomplete: {len(errors)} file errors, {len(expected - scanned)} unscanned files.")
        return 2

    # Bandit's raw JSON includes source code and issue messages that may contain values.
    # Only emit finding locations, IDs and classifications; never save the raw report.
    for finding in findings:
        print(f"{finding['filename']}:{finding['line_number']}: {finding['test_id']} "
              f"severity={finding['issue_severity']} confidence={finding['issue_confidence']}")
    print(f"Bandit scanned {len(expected)} Python files; {len(findings)} findings at medium-or-higher severity and confidence.")
    return 1 if findings or result.returncode else 0


if __name__ == "__main__":
    raise SystemExit(main())
