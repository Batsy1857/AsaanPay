# CI security gates — Milestone 3

The existing `tests` job and its push/PR triggers are preserved. Three independent
Ubuntu 24.04 jobs run alongside it, using the workflow's `contents: read` permission.
All nonzero scanner or installation exits fail their jobs. No job uses
`continue-on-error`, `--exit-zero`, automated dependency fixes, or a vulnerability
baseline that hides previous vulnerabilities. The Milestone 4 build/release job requires
`needs: [tests, bandit, gitleaks, pip-audit]`. The original four jobs and their
security policies are unchanged; see the appended image-scanning policy below.

| Job | Fixed scanner | Blocking policy |
| --- | --- | --- |
| `bandit` / Bandit SAST | Bandit **1.9.4**, Python 3.13 | Scan every Python file under `app/` and `scripts/`; block findings with both severity and confidence Medium or High (`-ll -ii`). Incomplete scans also fail. |
| `gitleaks` / Gitleaks history scan | Gitleaks CLI **8.30.1** | Block every finding except the eight individually reviewed GL-001 fingerprints below. Redact 100% of detected secret values. |
| `pip-audit` / Python dependency audit | pip-audit **2.10.1**, Python 3.13 | Audit `requirements.txt` and its resolved transitive dependencies; block every reported advisory, irrespective of severity or fix availability. No advisory ignores are configured. |

Bandit and pip-audit are installed in separate temporary virtual environments, not
in `requirements.txt` or the application image. Gitleaks is downloaded from its
fixed official release URL and checked against the committed Linux x64 SHA-256:
`551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb`.
Existing checkout/setup-python Actions remain pinned to their commit SHAs.

## Complete scans and safe output

- `.github/scripts/bandit_gate.py` runs `python -m bandit -r app scripts -ll -ii -f json`.
  Bandit 1.9.4 can exit zero while reporting a Python parse error. The wrapper checks
  file errors, source-file coverage, report structure, scanner error logging and
  exit status. It returns 1 for findings and a nonzero status for scanner failures.
  It prints only file/line, rule ID, severity and confidence; source snippets and
  potentially value-bearing issue messages are neither printed nor saved.
- Gitleaks checks out with `fetch-depth: 0`, rejects shallow/empty history, and scans
  all fetched refs with `--all --full-history --diff-merges=first-parent`. This includes
  merge changes relative to the first parent. It cannot recover deleted/unreachable
  server history that GitHub does not provide. Gitleaks 8.30.1 can return zero after
  a Git execution error; `.github/scripts/gitleaks_gate.sh` also rejects error-level
  diagnostics and missing/zero scanned-commit counts. The job uses `--redact=100`,
  uploads no secret reports, and does not print matching secret values. Error output
  is withheld; the wrapper deletes its temporary diagnostic file on exit.
- pip-audit resolves requirements rather than auditing its own scanner environment.
  `--strict` makes dependency-collection failures fatal; **it is not a severity
  filter**. Installation, collection, service/network and scanner execution errors
  must be investigated as failed checks, not treated as a clean vulnerability scan.
  JSON output records package versions, advisory IDs/aliases and available fixes,
  with advisory descriptions disabled. No automatic upgrades run.

## Baseline assessment

Inspected on **2026-11-17**, based on application commit `d8506fe`. The local repository
was not shallow and had 15 reachable commits across its local/remote refs. Gitleaks
reports patch-bearing commits; that counter need not equal `git rev-list --all --count`.

| Scanner | Actual baseline | Assessment and action |
| --- | --- | --- |
| Bandit policy scan | 9 Python files; zero Medium/High severity-and-confidence findings; zero file errors | Passing gate. No Bandit exception or application correction needed. |
| Bandit informational scan | **B404**, Low severity/High confidence, `scripts/benchmark.py:19`; **B603**, Low severity/High confidence, `scripts/benchmark.py:375` | Subprocess import and invocation used only for fixed `git rev-parse HEAD` / `git status --short` metadata commands, with an argument list and no shell. These are contextual warnings below the stated gate, not confirmed exploitable injection. Reassess if callers accept untrusted commands or enable a shell. No suppression added. |
| Gitleaks, before exceptions | Eight **generic-api-key** findings in historical benchmark exports | Reviewed synthetic idempotency identifiers, not authentication credentials. GL-001 allows only these exact historical findings; no benchmark data was edited. Gitleaks does not assign CVSS severity. |
| pip-audit | 17 resolved packages; zero known vulnerability advisories returned | No affected package/advisory or dependency upgrade identified. This is a dated advisory-service result, not proof that dependencies have no vulnerabilities. |

The resolved dependency snapshot was:

```text
fastapi==0.135.1          uvicorn==0.41.0          asyncpg==0.31.0
httpx==0.28.1            httpcore==1.0.9         annotated-doc==0.0.6
click==8.5.1             h11==0.16.0             pydantic==2.14.3
pydantic-core==2.50.4    annotated-types==0.8.0  starlette==1.10.0
anyio==4.16.0            idna==3.20              typing-extensions==4.16.0
typing-inspection==0.4.5 certifi==2026.10.23
```

Only the four direct requirements are pinned. Transitive resolution can change
between dates, platforms, test jobs, audits and image builds. This snapshot is
documentation, not a dependency lock. Scanner transitive dependencies also remain
resolver-selected. A reviewed dependency lock remains future work. Milestone 4 now audits the exact
built-image inventory; the Milestone 3 baseline made no application dependency changes.
`httpx` is included because the current Dockerfile installs the entire requirements
file, including the TestClient dependency.

## GL-001: reviewed synthetic benchmark identifiers

Finding ID: **generic-api-key**. Reviewed on **2026-11-17**. All eight entries belong
to commit `a915f5a1e69cfcc858649866f265f608ff0fc4bf`:

| File | Lines | Recorded field |
| --- | --- | --- |
| `benchmarks/results/milestone6-full/metadata.json` | 4 | `idempotency_key_prefix` |
| `benchmarks/results/milestone6-smoke/metadata.json` | 4 | `idempotency_key_prefix` |
| `benchmarks/results/milestone6-smoke/observations.jsonl` | 4 | `idempotency_key` |
| `benchmarks/results/milestone6-full/observations.jsonl` | 4, 7, 18, 21, 26 | `idempotency_key` |

`scripts/benchmark.py` generates these identifiers from a benchmark marker, run
timestamp and UUID, then appends the trial identity. They identify synthetic
checkout attempts; they do not authenticate to GitHub, AWS or a payment provider.
Their entropy and key-like field names trigger the generic rule.

`.gitleaksignore` contains only the eight exact commit/path/rule/line fingerprints.
No directory, file, regex class, entire rule or commit is excluded. Newly introduced
credentials in these same files remain subject to scanning. No secret values or
full matched strings are included in the exception file or this report.

Review conditions: reassess GL-001 whenever Gitleaks is upgraded, the provenance or
meaning of the recorded fields changes, or benchmark data is used outside this
synthetic simulator. New findings require independent review; do not extend this
list automatically or replace it with a benchmark-directory exclusion. If an entry
is discovered to contain a real credential, remove its exception and revoke/rotate
the credential before treating the incident as resolved.

## Reproduce the gates

Run from the repository root with Python 3.13. These commands use temporary tooling
and do not update the application's virtual environment or requirements:

```sh
ci_security_dir="$(mktemp -d)"
python3 -m venv "$ci_security_dir/tools"
"$ci_security_dir/tools/bin/python" -m pip install bandit==1.9.4 pip-audit==2.10.1

"$ci_security_dir/tools/bin/python" .github/scripts/bandit_gate.py
"$ci_security_dir/tools/bin/pip-audit" -r requirements.txt --strict \
  --progress-spinner off --timeout 30 --format json --desc off --aliases on
```

For Gitleaks on Linux x64, use the same pinned archive/checksum as CI:

```sh
curl --fail --silent --show-error --location \
  https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_linux_x64.tar.gz \
  --output "$ci_security_dir/gitleaks.tar.gz"
printf '%s  %s\n' \
  551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb \
  "$ci_security_dir/gitleaks.tar.gz" | sha256sum --check --strict
tar -xzf "$ci_security_dir/gitleaks.tar.gz" -C "$ci_security_dir" gitleaks
bash .github/scripts/gitleaks_gate.sh "$ci_security_dir/gitleaks"
```

For macOS ARM64, the corresponding official `gitleaks_8.30.1_darwin_arm64.tar.gz`
checksum is `b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5`;
verify it with `shasum -a 256` before execution. Do not use the Linux binary on macOS.
Local Git scanning includes committed history, not uncommitted working-tree edits.

## Local validation

Executed on **2026-11-17** with Python 3.13.2 on macOS ARM64, Docker's Linux
containers, and native Chrome 157.0.8103.51:

| Check | Result |
| --- | --- |
| Existing unit/component command, `RUN_DB_TESTS=0` | 61 discovered: 50 passed, 11 intentionally skipped database tests. |
| Full suite in a fresh Compose project, `RUN_DB_TESTS=1` | 61 passed, zero skips; the existing coverage guard confirmed all 11 database methods executed. Health and initialized-empty-schema checks passed first. |
| Existing frontend/browser runner | 43 passed, zero failures, against the disposable application. |
| Cleanup | Test containers, network, volume and unique image tag removed; saved test logs checked for the temporary password. |
| Final security gates | Bandit passed with complete coverage; Gitleaks passed with GL-001; pip-audit passed for all 17 resolved packages. |
| Workflow validation | actionlint 1.7.12 passed; YAML parsed; all 18 embedded shell blocks and the Gitleaks wrapper passed `bash -n`; the Bandit wrapper parsed successfully. ShellCheck was unavailable. The original tests job and top-level workflow configuration compare identically with `d8506fe`. |

Failure-path checks used temporary fixtures outside the repository:

- Bandit rejected an actual Medium/High-confidence finding, invalid Python,
  invalid reports, missing source-file coverage and an internal scanner error.
- Gitleaks detected a generated, nonfunctional credential-shaped marker with
  exit 1 and no marker value in output. Its wrapper rejected an actual Git
  execution error that Gitleaks itself reported with exit 0, an empty scan and
  a scanner failure; it also preserved a finding's exit 1.
- pip-audit returned exit 1 for a known-vulnerable `urllib3==1.26.5` query fixture,
  without installing it or changing application dependencies. Missing requirements
  and an unavailable advisory service with an empty cache also failed.

The official Linux x64 Gitleaks archive was downloaded and its workflow checksum
verified, but its Linux executable was not run on macOS. Local scanner execution
used the verified macOS ARM64 binary of the same version. The new jobs still need
a real Ubuntu 24.04 GitHub Actions run; local browser results use macOS Chrome.

## GitHub verification and remediation

After review, commit and push the changed workflow, helpers, exception file and docs
to `feature/aws-cicd-devsecops`. In the new **CI** run for that exact commit, verify
four successful jobs: **Python, PostgreSQL and browser tests**, **Bandit SAST**,
**Gitleaks history scan**, and **Python dependency audit**. Inspect versions, Bandit
coverage, Gitleaks history scanning, the dependency inventory, all 11 database
tests and all 43 browser checks. Local validation is not GitHub Actions validation.

Any new advisory or credible source finding requires investigation of affected
versions/code, applicability and available fixes. Application security corrections
or dependency upgrades that could affect payment behavior require review before
implementation. No such correction or upgrade was identified as necessary by this
baseline. Milestone 3 added no AWS, deployment, release artifacts or Trivy functionality;
the Milestone 4 additions are described below.

References: [Bandit CLI](https://bandit.readthedocs.io/en/latest/man/bandit.html),
[Gitleaks CLI and fingerprint exceptions](https://github.com/gitleaks/gitleaks),
[pip-audit usage and exit codes](https://github.com/pypa/pip-audit).


## Milestone 4: release image vulnerability gate

Trivy **0.69.3** scans the exact built `linux/amd64` image ID, including OS and
Python application packages. `.github/scripts/install_trivy.sh` downloads only
fixed official release assets and checks committed SHA-256 pins before executing:

- Linux x64: `1816b632dfe529869c740c0913e36bd1629cb7688bd5634f4a858c1d57c88b75`
- macOS ARM64 (local validation): `a2f2179afd4f8bb265ca3c7aefb56a666bc4a9a411663bc0f22c3549fbc643a5`

This is an explicitly selected version, not `latest`. Aqua's
[security advisory](https://github.com/aquasecurity/trivy/security/advisories/GHSA-69fq-xp46-6x23)
identifies v0.69.3 as unaffected by the March 2026 compromised releases and
protected by immutable releases. No Trivy setup/action tags are executed.
Checksums were obtained from the
[official v0.69.3 checksums](https://github.com/aquasecurity/trivy/releases/download/v0.69.3/trivy_0.69.3_checksums.txt).
Review scanner upgrades explicitly, along with their provenance and policy behavior.

**Blocking policy:** every OS or application **High/Critical finding with a
published fixed version** blocks packaging and upload. Unfixed High/Critical
findings remain visible as `REVIEW`; Medium/Low/Unknown counts and fix availability
are also shown. Unfixed does not mean safe or inapplicable. Review exposure,
mitigations and upstream fixes before a later deployment. No Trivy exceptions,
ignore entries, VEX statements or directory/package exclusions are configured.
Any future exception must identify advisory, exact package/version, scope,
justification, owner, expiry and compensating control; it requires explicit review.

The wrapper starts with a fresh cache and downloads the vulnerability database
from the official `ghcr.io/aquasecurity/trivy-db:2` repository on every run. Download
failure fails the check, including when an old cache exists elsewhere. That one
fresh database is used for the image scan; `--skip-db-update` only prevents a second
download within the same invocation. The downloaded database timestamp/version is
saved in `db-metadata.json` alongside the complete `trivy.json` report.

The CLI returns its execution status and the Python policy gate evaluates the
unfiltered report. Successful scanner execution alone never grants approval.
Scanner failures, malformed reports, unexpected image identity, unsupported OS,
and missing OS or Python application inventories fail closed. A receipt bound to
the image ID is written only after policy success, and packaging requires it.
The readable summary and full findings are uploaded separately for 14 days even
when the vulnerability gate fails. Release packages upload only after every check
and reload verification succeeds. No `continue-on-error` or severity suppression
is used; the existing stricter pip-audit policy still blocks *every* Python advisory.

See [release operations](releases.md) and [actual local findings](milestone-4-report.md)
for reproducible commands and the dated scan result. No dependency upgrade is
performed automatically. If a fix requires an application, Python or distribution
upgrade, report the affected packages and compatibility risks before changing it.
