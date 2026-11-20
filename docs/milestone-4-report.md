# Milestone 4 implementation report

Implemented and locally validated on **2026-11-20 (Asia/Karachi)** on
`feature/aws-cicd-devsecops`, starting at commit
`5a62f44db9eb92dc8b32310c20d7ca1ca650f549`. Changes remain uncommitted and unpushed.
No AWS publication, infrastructure or deployment was performed.

## 1. Files created or modified

| Area | Created | Modified |
| --- | --- | --- |
| Version reporting | `VERSION`, `app/version.py`, `app/static/version.js` | `app/main.py`, `app/static/index.html`, `app/static/styles.css` |
| Build and release | `scripts/release.py` | `Dockerfile`, `.dockerignore`, `.gitignore`, `.github/workflows/ci.yml` |
| Image security | `.github/scripts/install_trivy.sh`, `.github/scripts/trivy_gate.sh`, `scripts/trivy_policy.py` | — |
| Tests | `tests/test_version.py`, `tests/test_release.py`, `.github/scripts/test_trivy_gate.py` | `tests/frontend_tests.js`, `tests/run_frontend.py`, `tests/test_static.py` |
| Documentation | `docs/releases.md`, this report | `README.md`, `docs/security.md` |

Ignored local outputs are in `release-output/`; these must not be committed.
The original four workflow jobs and top-level triggers/permissions compare
identically with HEAD. Requirements, Compose configuration, payment logic,
idempotency, PostgreSQL schema and benchmark files are unchanged.

## 2. Version endpoint and UI

`GET /api/version` reads image-owned metadata, returning the application version
and full SHA. Metadata includes and validates the run ID/attempt and consistent
release ID. It is root-owned and mode `0444`; release containers run as UID/GID
10001. Runtime environment variables cannot change it. Release startup requires
valid metadata; local development without the file returns explicit `dev` values.
Malformed existing metadata produces HTTP 503 rather than development fallback.

The footer badge fetches the endpoint independently, displays version and seven
SHA characters with `textContent`, and handles HTTP/network/JSON/validation/timeout
failure with a small unavailable message. Checkout and Explorer remain usable.
The existing healthy response and `rule_set_version="sandbox_v1"` are unchanged.

## 3. Docker build strategy

The new Ubuntu 24.04 `build-release` job depends on `tests`, `bandit`, `gitleaks`,
and `pip-audit`. It has `contents: read` only. Release IDs are:

```text
<version>-g<full SHA>-r<Actions run ID>-a<run attempt>
```

Each candidate is built once using `--platform linux/amd64 --target release`.
Scanning, smoke tests, save and reload verification refer to that same image ID.
The default development target still supports Compose and its in-container tests.
The production target omits tests/scripts, removes pip after dependency installation,
and applies the narrowly scoped Debian zlib security patch described below.

Two local candidates were built: attempt 1 exposed blocking vulnerabilities;
attempt 2 contains the remediation. Attempt 2 was never rebuilt during its scan,
smoke, packaging or reload verification. Neither local candidate is a committed
GitHub release: its manifest explicitly records `local_validation: true` and
`source_dirty: true`, and its numeric run ID is a local validation identifier.

## 4. Trivy results and findings

Trivy **0.69.3**, verified against committed official-release SHA-256 pins, scanned
OS and Python packages with a fresh official vulnerability database. The final
inventory contains **87 Debian packages and 17 Python packages**. DB schema is 2;
`UpdatedAt` is `2026-11-20T13:11:33.075919767Z` (18:11:33 PKT), and the final scan
downloaded it at `2026-11-20T15:52:43.615992Z` (20:52:43 PKT).

| Severity | Initial fixable | Final fixable | Final unfixed |
| --- | ---: | ---: | ---: |
| Critical | 0 | 0 | 0 |
| High | 6 | 0 | 47 |
| Medium | 3 | 0 | 109 |
| Low | 1 | 0 | 67 |
| Unknown | 0 | 0 | 9 |

The gate **passed** after remediation: zero fixable High/Critical findings.
There are **zero final Python vulnerability findings**. The 232 remaining findings
are OS package/advisory pairs; this does not mean the image is vulnerability-free.
No Trivy exceptions, ignore entries or threshold changes were introduced.

Initial blocking findings and remediation:

| Package/inventory | Advisory | Installed → available fix | Applied action and compatibility considerations |
| --- | --- | --- | --- |
| Debian `zlib1g` | CVE-2026-85091 | `1:1.3.dfsg+really1.3.1-1+b1` → `1:1.3.dfsg+really1.3.1-1+deb13u1` | Targeted Debian security patch, no distribution/ABI major change. Full API, database and browser regressions passed. |
| pip bundled `msgpack` inventory | CVE-2026-114613; GHSA-6v7p-g79w-8964 | `1.1.2` → `1.2.3`; `1.2.1` | Removed pip and its bundled inventory from production after installing requirements. |
| pip bundled `setuptools` inventory | CVE-2025-47273 | `70.3.0` → `78.1.1` | Removed with pip; application does not require this installer tool at runtime. |
| pip bundled `urllib3` inventory | CVE-2026-97687; CVE-2026-97689 | `2.7.0` → `2.8.0` | Removed with pip; application dependency pins remain unchanged. |

The pip findings came from the scanner's bundled SBOM inventory. Removing the
entire unused installer avoids partial manual edits of vendored libraries or
in-place application dependency upgrades. Runtime package installation is now
unavailable by design; development retains pip. The affected packages and this
tradeoff were reported before remediation. An alternative would be a reviewed
Python base/pip update whose vendor inventory resolves these findings; that would
need a new candidate and the same complete validation.

**Unfixed High findings still requiring review before deployment:**

| Advisory | Affected packages in the image |
| --- | --- |
| CVE-2025-69720 | `libncursesw6`, `libtinfo6`, `ncurses-base`, `ncurses-bin` |
| CVE-2026-119373, CVE-2026-119374, CVE-2026-119379 | `libsqlite3-0` |
| CVE-2026-16742 | `libsystemd0`, `libudev1` |
| CVE-2026-54369 | `libacl1` |
| CVE-2026-76642, CVE-2026-78408, CVE-2026-78409, CVE-2026-78410 | `bsdutils`, `libblkid1`, `liblastlog2-2`, `libmount1`, `libsmartcols1`, `libuuid1`, `login`, `mount`, `util-linux` |
| CVE-2026-9538 | `perl-base` |

These are 47 package/advisory findings across 18 packages and 11 distinct advisory
IDs. They remain visible under the documented fixable High/Critical policy; no
claim of non-exploitability is made. Review exposure and upstream patches. Switching
distributions or removing core OS packages would broaden compatibility risk and
was not done. The full reports and readable summaries are saved under
`release-output/validation/security/` and `rejected-candidate-security/`.

## 5. Smoke-test results

Passed on the original candidate, the remediated candidate, and the loaded archive:

- PostgreSQL 17 initialized the existing schema with an empty `payment_attempts` table.
- `/api/health` returned exactly `{"status":"ok","database":"connected"}`.
- `/api/version` matched the image file, labels and expected release identity.
- `/`, static HTML, CSS and all three JavaScript assets were served correctly.
- A synthetic PKR 100 approved domestic concurrent checkout reached `SUCCEEDED`.
- The expected record, rule set, idempotency key and completion timestamp persisted.
- Stored retrieval succeeded; replay returned the same ID with no second record.

Each release smoke run used UUID-named resources and a tmpfs PostgreSQL data
folder, with no database volume. All created containers/networks were removed.
The separate development Compose validation also removed only its new disposable
volume and confirmed all 25 pre-existing volumes were preserved.

## 6. Verified local release package

```text
release-output/
  asaanpay-release-1.0.4-g5a62f44db9eb92dc8b32310c20d7ca1ca650f549-r2026112001-a2/
    image.tar.gz
    manifest.json
    SHA256SUMS
  validation/  # local evidence, outside the three-file release package
```

Application version: `1.0.4`. Platform: `linux/amd64`. Compressed image: approximately
50 MiB. Docker image ID:

```text
sha256:ed23dacb7b955f1a48b7449b736ee5612b28cd1e65769630f7ba6dbcf4cc511c
```

SHA-256 checksums:

```text
e301ee5350d29dd19967ecca27ea2828f25b4deccbc8691ad4c9e33550a8def0  image.tar.gz
3016640ddf0f33a3e80e353be1cbe0d909f1d01a943a1c93b093f5481362538f  manifest.json
```

Both checksums, compressed `docker load`, image identity/platform/labels/file and
the loaded image's live version/checkout smoke were verified successfully without
rebuilding. The image's `/app` files were inspected: only application code/assets,
requirements and generated metadata were present. No `.env`, secrets, private
keys, development database data, tests or scanner tooling were included there.
The ignored package is **local validation evidence, not deployable provenance**.

## 7. Completed local validation

| Check | Outcome |
| --- | --- |
| Python 3.13 host unit/component suite | 79 discovered: 68 passed, 11 intentionally skipped PostgreSQL tests |
| Full suite on exact final amd64 release image with disposable PostgreSQL | 79 passed; zero skips; all 11 database methods confirmed |
| Default Compose development build and PostgreSQL suite | 79 passed; zero skips |
| Browser suite against both final release API and development API | 46 passed, zero failures in each run |
| Scanner/DB failure wrapper test | Passed, covering both failing DB download and failing scanner, including stale-approval removal |
| Real Trivy unavailable-DB check | Exit 1 with an empty cache and an intentionally unavailable local registry |
| Bandit 1.9.4 existing policy | Complete scan of 12 Python files; zero blocking findings |
| Gitleaks 8.30.1 existing history policy | Passed using unchanged GL-001; 15 patch-bearing commits reported |
| Supplemental Gitleaks changed-source scan | Passed; no new secret findings |
| pip-audit 2.10.1 existing strict policy | 17 resolved packages; zero advisories |
| Final target-platform Docker build | Passed; Python 3.13.18, Linux amd64, Debian 13.7 |
| Trivy final policy | Passed; zero actionable High/Critical findings; unfixed findings retained above |
| Metadata runtime protections | Environment overrides ineffective; non-root writes denied; malformed mounted metadata rejected before DB startup |
| Artifact checksums, load and loaded-image smoke | Passed |
| Negative integrity/policy cases | Tampered archive/manifest, wrong ID/platform/labels, unsafe/missing/duplicate checksums, dirty CI provenance, incomplete/wrong-image scan and blocking findings rejected |
| Workflow and shell validation | actionlint 1.7.12, ShellCheck 0.11.0, YAML parse, all 26 embedded Bash blocks and shell wrappers passed |
| Existing-job preservation | All four original jobs and top-level workflow configuration unchanged |

Local host: macOS ARM64, Python 3.13.2 and native Chrome. Docker exercised the release
as `linux/amd64` using Docker Desktop emulation; development used native Linux ARM64.
The existing browser runner serves checkout assets from source, while independent
smoke requests verify actual release-served assets. The Linux Trivy archive was also downloaded and its committed SHA-256 verified;
execution locally used the verified
macOS ARM64 binary of the same pinned version.

## 8. Items requiring review / GitHub validation

- Review the 47 unfixed High OS findings before any later deployment. The current
  policy deliberately distinguishes published fixes from unfixed vulnerabilities.
- Review the narrow zlib patch and production pip removal. No application dependency
  pins, payment behavior, schema or benchmark output changed.
- Base tags and transitive dependency resolution remain mutable. The artifact
  identifies the exact tested image, but rebuilding later is not bit-reproducible.
- Checksums are integrity checks, not signatures. Later AWS work must trust the
  successful Actions source and reject local/dirty provenance flags.
- The Ubuntu 24.04 hosted job, native amd64 runtime, Linux Trivy execution and actual
  GitHub artifact upload/download still require a pushed commit and hosted run.
  Local validation does not establish that GitHub Actions is green.

## 9. Commit and push instructions (not executed)

Review the diff and report, then run from the repository root:

```sh
git diff --check
git status --short
git add .dockerignore .gitignore Dockerfile VERSION README.md \
  .github/workflows/ci.yml .github/scripts/install_trivy.sh \
  .github/scripts/trivy_gate.sh .github/scripts/test_trivy_gate.py \
  app/main.py app/version.py app/static/index.html app/static/styles.css app/static/version.js \
  scripts/release.py scripts/trivy_policy.py \
  tests/test_version.py tests/test_release.py tests/test_static.py \
  tests/frontend_tests.js tests/run_frontend.py \
  docs/security.md docs/releases.md docs/milestone-4-report.md
git diff --cached --stat
git commit -m "Add versioned, scanned Docker release artifacts"
git push origin feature/aws-cicd-devsecops
```

Do not stage `release-output/`; it is ignored. The later Actions build will embed
the newly committed SHA and real run/attempt rather than this local test identity.

## 10. Verify the new GitHub job

Open the **CI** run for that pushed commit in `Batsy1857/AsaanPay`. Confirm the four
existing checks pass and then **Versioned image, Trivy and release artifact** runs.
Check one release build, Linux amd64 identity, the pinned scanner verification,
fresh DB timestamp, readable policy summary, smoke result and archive reload result.
Expect a unique `asaanpay-release-<release-id>` artifact retained for 14 days and
`image-security-<run-id>-<attempt>` evidence. Download the release package and run
`sha256sum --check --strict SHA256SUMS`, `docker load --input image.tar.gz`, and
compare its ID/metadata with the manifest. CI provenance flags must both be false.
A failed gate must prevent release artifact upload. See [release operations](releases.md)
for the complete verification commands and the later EC2 consumption contract.
