#!/usr/bin/env bash
# Gitleaks 8.30.1 can return zero after a git-log error. Reject those partial scans.
set -euo pipefail

gitleaks="${1:-gitleaks}"
test "$(git rev-parse --is-shallow-repository)" = false
test "$(git rev-list --all --count)" -gt 0

scan_log="$(mktemp "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/gitleaks-gate.XXXXXX")"
trap 'rm -f "$scan_log"' EXIT
status=0
"$gitleaks" git --redact=100 --no-color --no-banner --log-level info \
  --log-opts="--all --full-history --diff-merges=first-parent" \
  --timeout 300 --exit-code 1 . >"$scan_log" 2>&1 || status=$?

if [ "$status" -gt 1 ] || grep -Eq '(^|[[:space:]])(ERR|FTL|PNC)([[:space:]]|$)' "$scan_log"; then
  echo "Gitleaks execution error (exit $status); raw diagnostics withheld to avoid disclosing values."
  exit 2
fi
if ! grep -Eq '[1-9][0-9]* commits? scanned\.' "$scan_log"; then
  echo "Gitleaks scan incomplete: no successful, nonempty history scan was reported."
  exit 2
fi

# Only the explicitly redacted scanner output is displayed. No report is uploaded.
cat "$scan_log"
exit "$status"
